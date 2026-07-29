// =============================================================================
// tb_het_pair.sv — the HETEROGENEOUS pair: one real `nanosoc_eth_chiplet` die
// and one real `nanosoc_compute_chiplet` die, cross-wired through the TideLink
// GPIO-PHY pads exactly as the J21 ribbon will join two KR260 boards.
//
// A joint work commissioned on behalf of SoC Labs, under Arm Academic Access license.
//
// Copyright 2026, SoC Labs (www.soclabs.org)
// =============================================================================
// This is the pre-silicon gate for the two-board bring-up. Today the ethernet
// chiplet is only ever paired with ITSELF (verif/g2_soc_pair, die_a/die_b flip)
// and the compute chiplet only ever with ITSELF; nobody has put the two REAL
// integration tops in one simulation. Everything asymmetric between the two
// designs — address map, aperture base, inbound target set, strap polarity,
// TideLink revision — is invisible to a homogeneous pair by construction.
//
// STRUCTURE (mirrors verif/g2_soc_pair/tb_g2_soc_pair.sv, which is the model):
//
//   u_dieE : nanosoc_eth_chiplet       u_dieC : nanosoc_compute_chiplet
//     pad_clk_tx / pad_tx      ---->     pad_clk_rx_0 / pad_rx_0
//     pad_clk_rx / pad_rx      <----     pad_clk_tx_0 / pad_tx_0
//     i2c_{scl,sda}_{i,o,t}    <--->     i2c_{scl,sda}_{i,o,t}_0   (wired-AND)
//     role_strap_i = 0 (master)          role_strap_i_0 = 1 (slave)
//
// The compute die's LINK 1 is a second, unconnected TideLink. It is held
// inactive (no RX activity, benign straps) and its status pins are brought out
// so a test can assert it stays down — a link-1 that spuriously trains would be
// a real bug on a board where only link 0 is cabled.
//
// ------------------------------- READ THIS ----------------------------------
// TWO STRUCTURAL ASYMMETRIES vs the homogeneous pair. Both are DESIGN facts,
// not testbench shortcomings. See docs/SIM_PLAN.md for the full assessment.
//
//  1. NO STIMULUS PORT ON THE COMPUTE DIE.
//     `nanosoc_eth_chiplet` exports `eth_ss_0_*`, an external AHB slave that
//     reaches the SoC matrix — that is how g2_soc_pair brings BOTH its dies up
//     firmware-free (AHB writes to 0x2E03_xxxx become TideLink APB writes).
//     `nanosoc_compute_chiplet` exports NO AHB or APB port at all: its only
//     boundary ingress is QSPI flash, UART and the SWJ-DP. So the compute die's
//     TideLink APB (role lock, LL bootstrap, CAM) is UNREACHABLE from this
//     testbench. The eth side is driven properly below; the compute side has
//     only its straps. What that costs is spelled out in the test file.
//
//  2. THE APERTURE BASES DIFFER.
//     eth  d2d window 0x2E000000..0x2FFFFFFF -> peer aperture 0x2F......
//     cmp  d2d0 window 0x40000000..0x4FFFFFFF -> peer aperture 0x41......
//     `chiplet_d2d_decode` is byte-identical in both repos and splits its window
//     on haddr[24], so it re-lands verbatim on the compute base — but SHIFTED.
//     The compute firmware's COMPUTE_D2D0_PEER_BASE (0x40000000) therefore
//     decodes as the TX aperture, not the peer aperture. That is a compute-die
//     RTL/firmware defect this testbench documents but must not paper over.
//
// The pad cross-wire, the `pad_skid`, the I2C wired-AND and the flash models are
// lifted from verif/g2_soc_pair/tb_g2_soc_pair.sv unchanged. Nothing here forks
// either chiplet: both are instantiated as shipped.
// =============================================================================
`timescale 1ns/1ps

module tb_het_pair #(
`ifdef TB_TOP_SKID_BITS
    parameter int SKID_BITS = `TB_TOP_SKID_BITS,
`else
    parameter int SKID_BITS = 0,
`endif
    parameter int NUM_PHY_LANES = 8,
    parameter     FCLK_PERIOD_NS = 20,   // 50 MHz application/AHB clock
    parameter     REF_PERIOD_NS  = 8     // 125 MHz Wlink PLL reference
);

    // ---------------------------------------------------------------------
    // Shared clocks. Generated in-tb (as g2_soc_pair does) so a cocotb test
    // drives transactions synchronous to `sys_fclk` without owning a clock.
    // `ref_clk` is the Wlink PLL reference, shared by both dies — on a bench
    // each board has its own oscillator, so a later variant should skew these.
    // ---------------------------------------------------------------------
    reg sys_fclk = 1'b0;
    always #(FCLK_PERIOD_NS/2.0) sys_fclk = ~sys_fclk;

    reg ref_clk = 1'b0;
    always #(REF_PERIOD_NS/2.0) ref_clk = ~ref_clk;

    // Per-die system reset. Separate so a test can skew one die's reset against
    // the other (the far-die-in-reset wedge case).
    reg e_sysresetn = 1'b0;
    reg c_sysresetn = 1'b0;
    initial begin
        e_sysresetn = 1'b0;
        c_sysresetn = 1'b0;
        #200;
        e_sysresetn = 1'b1;
        c_sysresetn = 1'b1;
    end

    // Per-die "pad drive enabled" gates. A die held in reset must not X-poison
    // the live die through the pads; squash its pad drive to 0. Default 1.
    reg e_pad_en = 1'b1;
    reg c_pad_en = 1'b1;

    // =====================================================================
    // PHY pads, cross-wired through pad_skid (SKID_BITS=0 => passthrough).
    // Ethernet die's single ribbon <-> compute die's LINK 0 ribbon.
    // =====================================================================
    wire                     e_pad_clk_tx, c_pad_clk_tx_0;
    wire [NUM_PHY_LANES-1:0] e_pad_tx,     c_pad_tx_0;
    wire                     e_pad_clk_tx_skid, c_pad_clk_tx_0_skid;
    wire [NUM_PHY_LANES-1:0] e_pad_tx_skid,     c_pad_tx_0_skid;

    pad_skid #(.SKID_BITS(SKID_BITS), .LANES(NUM_PHY_LANES)) u_skid_e2c (
        .pad_clk_in (e_pad_clk_tx),      .pad_data_in (e_pad_tx),
        .pad_clk_out(e_pad_clk_tx_skid), .pad_data_out(e_pad_tx_skid));

    pad_skid #(.SKID_BITS(SKID_BITS), .LANES(NUM_PHY_LANES)) u_skid_c2e (
        .pad_clk_in (c_pad_clk_tx_0),      .pad_data_in (c_pad_tx_0),
        .pad_clk_out(c_pad_clk_tx_0_skid), .pad_data_out(c_pad_tx_0_skid));

    // =====================================================================
    // I2C sideband: open-drain wired-AND with pull-ups. The eth die's single
    // I2C pair against the compute die's LINK 0 pair.
    // =====================================================================
    wire e_i2c_scl_o, e_i2c_scl_t, e_i2c_sda_o, e_i2c_sda_t;
    wire c_i2c_scl_o_0, c_i2c_scl_t_0, c_i2c_sda_o_0, c_i2c_sda_t_0;
    wire i2c_scl = (e_i2c_scl_t ? 1'b1 : e_i2c_scl_o) & (c_i2c_scl_t_0 ? 1'b1 : c_i2c_scl_o_0);
    wire i2c_sda = (e_i2c_sda_t ? 1'b1 : e_i2c_sda_o) & (c_i2c_sda_t_0 ? 1'b1 : c_i2c_sda_o_0);

    // =====================================================================
    // Ethernet die eth_ss_0 external AHB master port (cocotb-driven). Reaches
    // the top matrix through the eth subsystem `system` passthrough, hence the
    // D2D window (link bring-up APB @0x2E03xxxx, peer aperture @0x2F......) and
    // shared_sram_0 (@0x2D......) without firmware.
    //
    // THE COMPUTE DIE HAS NO COUNTERPART. See the header.
    // =====================================================================
    reg  [31:0] e_eth_ss_0_haddr  = 32'h0;
    reg   [1:0] e_eth_ss_0_htrans = 2'b00;
    reg         e_eth_ss_0_hwrite = 1'b0;
    reg   [2:0] e_eth_ss_0_hsize  = 3'b010;
    reg   [2:0] e_eth_ss_0_hburst = 3'b000;
    reg   [3:0] e_eth_ss_0_hprot  = 4'h0;
    reg  [31:0] e_eth_ss_0_hwdata = 32'h0;
    reg         e_eth_ss_0_hmastlock = 1'b0;
    wire [31:0] e_eth_ss_0_hrdata;
    wire        e_eth_ss_0_hready;
    wire        e_eth_ss_0_hresp;

    // =====================================================================
    // Ethernet die boundary observability (named so cocotb can read them and so
    // the elaborator does not warn on open outputs).
    // =====================================================================
    wire        e_sys_poresetn, e_sys_hclk, e_sys_hresetn;
    wire        e_network_core_txev, e_network_core_lockup, e_network_core_sysresetreq;
    wire        e_network_core_sleeping, e_network_core_sleepdeep;
    wire        e_chip_core_txev, e_chip_core_lockup, e_chip_core_sysresetreq;
    wire        e_chip_core_sleeping, e_chip_core_sleepdeep;
    wire        e_dap_swdo, e_dap_swdoen, e_dap_tdo, e_dap_ntdoen;
    wire  [1:0] e_rmii_txd;  wire e_rmii_tx_en;
    wire        e_mdc_pad_o, e_md_pad_o, e_md_padoe_o;
    wire        e_uart_txd, e_chip_core_uart_txd, e_chip_core_wdog_reset;
    wire [31:0] e_rtc_time_ptp_ns;  wire [47:0] e_rtc_time_ptp_sec;  wire e_rtc_time_one_pps;
    wire        e_eth_irq, e_phc_pps_out, e_phc_pps_irq, e_phc_alarm_irq, e_ha1588_servo_locked;
    wire        e_spi_sclk, e_spi_mosi;  wire [2:0] e_spi_ss;
    wire  [6:0] e_hostio4_p1_out, e_hostio4_p1_outen;
    wire        e_scan_out;
    wire        e_link_active_o, e_d2d_reset_o, e_role_is_master_o, e_role_locked_o;
    wire        e_servo_locked_o;  wire [12:0] e_tl_ewma_credit_o;  wire e_tidechart_irq_o;

    // =====================================================================
    // Compute die boundary observability.
    // =====================================================================
    wire        c_sys_poresetn, c_sys_hclk, c_sys_hresetn;
    wire        c_uart_txd, c_phc_pps_out;
    wire        c_dap_swdo, c_dap_swdoen, c_dap_tdo, c_dap_ntdoen;
    wire        c_scan_out;
    // Link 0 — the cabled link.
    wire        c_link_active_o_0, c_d2d_reset_o_0, c_role_is_master_o_0, c_role_locked_o_0;
    wire        c_servo_locked_o_0;  wire [12:0] c_tl_ewma_credit_o_0;
    // Link 1 — deliberately NOT cabled. A test asserts it stays down.
    wire                     c_pad_clk_tx_1;
    wire [NUM_PHY_LANES-1:0] c_pad_tx_1;
    wire        c_i2c_scl_o_1, c_i2c_scl_t_1, c_i2c_sda_o_1, c_i2c_sda_t_1;
    wire        c_link_active_o_1, c_d2d_reset_o_1, c_role_is_master_o_1, c_role_locked_o_1;
    wire        c_servo_locked_o_1;  wire [12:0] c_tl_ewma_credit_o_1;
    wire        c_tidechart_irq_o;

    // =====================================================================
    // Per-die QSPI flash. Unprogrammed => each SoC's boot core reads 0xFF for
    // the BOOT-table magic and halts, keeping both buses free. Same tri-state
    // bridge and flash model both repos' benches use.
    // =====================================================================
    wire       e_qspi_sclk, e_qspi_csn;
    wire [3:0] e_qspi_io_o, e_qspi_io_e, e_qspi_io_i;
    wire [3:0] e_spi_io;
    wire       c_qspi_sclk, c_qspi_csn;
    wire [3:0] c_qspi_io_o, c_qspi_io_e, c_qspi_io_i;
    wire [3:0] c_spi_io;

    genvar gi;
    generate
        for (gi = 0; gi < 4; gi = gi + 1) begin : g_qspi_iobuf
            assign e_spi_io[gi] = e_qspi_io_e[gi] ? e_qspi_io_o[gi] : 1'bz;
            assign c_spi_io[gi] = c_qspi_io_e[gi] ? c_qspi_io_o[gi] : 1'bz;
        end
    endgenerate
    assign e_qspi_io_i = e_spi_io;
    assign c_qspi_io_i = c_spi_io;

    // =====================================================================
    // DIE E — the ETHERNET chiplet. Link MASTER (role_strap_i = 0).
    // nego_priority high so it wins the tiebreak; puf_seed distinct from die C.
    // =====================================================================
    nanosoc_eth_chiplet #(.NUM_PHY_LANES(NUM_PHY_LANES)) u_dieE (
        .sys_fclk            (sys_fclk),
        .sys_sysresetn       (e_sysresetn),
        .sys_poresetn        (e_sys_poresetn),
        .sys_hclk            (e_sys_hclk),
        .sys_hresetn         (e_sys_hresetn),
        .sys_scanenable      (1'b0),
        .sys_testmode        (1'b0),
        .sys_sysresetreq     (1'b0),

        .eth_ss_0_haddr      (e_eth_ss_0_haddr),
        .eth_ss_0_htrans     (e_eth_ss_0_htrans),
        .eth_ss_0_hwrite     (e_eth_ss_0_hwrite),
        .eth_ss_0_hsize      (e_eth_ss_0_hsize),
        .eth_ss_0_hburst     (e_eth_ss_0_hburst),
        .eth_ss_0_hprot      (e_eth_ss_0_hprot),
        .eth_ss_0_hwdata     (e_eth_ss_0_hwdata),
        .eth_ss_0_hmastlock  (e_eth_ss_0_hmastlock),
        .eth_ss_0_hrdata     (e_eth_ss_0_hrdata),
        .eth_ss_0_hready     (e_eth_ss_0_hready),
        .eth_ss_0_hresp      (e_eth_ss_0_hresp),

        .network_core_pmuenable (1'b0),
        .chip_core_pmuenable    (1'b0),
        .network_core_nmi       (1'b0),
        .network_core_txev      (e_network_core_txev),
        .network_core_rxev      (1'b0),
        .network_core_lockup    (e_network_core_lockup),
        .network_core_sysresetreq(e_network_core_sysresetreq),
        .network_core_sleeping  (e_network_core_sleeping),
        .network_core_sleepdeep (e_network_core_sleepdeep),
        .chip_core_nmi          (1'b0),
        .chip_core_txev         (e_chip_core_txev),
        .chip_core_rxev         (1'b0),
        .chip_core_lockup       (e_chip_core_lockup),
        .chip_core_sysresetreq  (e_chip_core_sysresetreq),
        .chip_core_sleeping     (e_chip_core_sleeping),
        .chip_core_sleepdeep    (e_chip_core_sleepdeep),

        .dap_swclktck        (1'b0),
        .dap_swditms         (1'b1),
        .dap_swdo            (e_dap_swdo),
        .dap_swdoen          (e_dap_swdoen),
        .dap_tdi             (1'b0),
        .dap_tdo             (e_dap_tdo),
        .dap_ntdoen          (e_dap_ntdoen),
        .dap_ntrst           (1'b1),
        .dap_npotrst         (e_sysresetn),
        .dap_swj_enable      (1'b1),

        .rmii_ref_clk        (1'b0),
        .rmii_txd            (e_rmii_txd),
        .rmii_tx_en          (e_rmii_tx_en),
        .rmii_rxd            (2'b0),
        .rmii_crs_dv         (1'b0),
        .md_pad_i            (1'b1),
        .mdc_pad_o           (e_mdc_pad_o),
        .md_pad_o            (e_md_pad_o),
        .md_padoe_o          (e_md_padoe_o),

        .uart_rxd            (1'b1),
        .uart_txd            (e_uart_txd),
        .chip_core_uart_rxd  (1'b1),
        .chip_core_uart_txd  (e_chip_core_uart_txd),
        .chip_core_wdog_reset(e_chip_core_wdog_reset),

        .rtc_clk             (sys_fclk),
        .rtc_time_ptp_ns     (e_rtc_time_ptp_ns),
        .rtc_time_ptp_sec    (e_rtc_time_ptp_sec),
        .rtc_time_one_pps    (e_rtc_time_one_pps),

        .eth_irq             (e_eth_irq),
        .phc_pps_out         (e_phc_pps_out),
        .phc_pps_irq         (e_phc_pps_irq),
        .phc_alarm_irq       (e_phc_alarm_irq),
        .ha1588_servo_locked (e_ha1588_servo_locked),

        .qspi_sclk           (e_qspi_sclk),
        .qspi_csn            (e_qspi_csn),
        .qspi_io_o           (e_qspi_io_o),
        .qspi_io_i           (e_qspi_io_i),
        .qspi_io_e           (e_qspi_io_e),

        .spi_sclk            (e_spi_sclk),
        .spi_mosi            (e_spi_mosi),
        .spi_miso            (1'b0),
        .spi_ss              (e_spi_ss),

        .hostio4_p1_in       (7'h0),
        .hostio4_p1_out      (e_hostio4_p1_out),
        .hostio4_p1_outen    (e_hostio4_p1_outen),

        // --- TideLink PHY pads: RX comes from the COMPUTE die's link 0 ---
        .pad_clk_tx          (e_pad_clk_tx),
        .pad_tx              (e_pad_tx),
        .pad_clk_rx          (c_pad_clk_tx_0_skid & c_pad_en),
        .pad_rx              (c_pad_tx_0_skid & {NUM_PHY_LANES{c_pad_en}}),
        .user_ref_clk        (ref_clk),
        .idelay_ref_clk      (1'b0),

        // --- I2C sideband + role strap ---
        .i2c_scl_i           (i2c_scl),
        .i2c_scl_o           (e_i2c_scl_o),
        .i2c_scl_t           (e_i2c_scl_t),
        .i2c_sda_i           (i2c_sda),
        .i2c_sda_o           (e_i2c_sda_o),
        .i2c_sda_t           (e_i2c_sda_t),
        .role_strap_i        (1'b0),          // ethernet die drives the link (master)

        // --- Link bring-up straps ---
        .nego_priority_i     (16'h8000),
        .mask_hs_bypass_i    (1'b1),
        .apb_debug_unlock_i  (1'b1),
        .puf_seed            (16'hA5A5),
        .puf_ready           (1'b1),

        // --- DFT ---
        .scan_mode           (1'b0),
        .scan_asyncrst_ctrl  (1'b0),
        .scan_clk            (1'b0),
        .scan_shift          (1'b0),
        .scan_in             (1'b0),
        .scan_out            (e_scan_out),

        // --- Status / observability ---
        .link_active_o       (e_link_active_o),
        .d2d_reset_o         (e_d2d_reset_o),
        .role_is_master_o    (e_role_is_master_o),
        .role_locked_o       (e_role_locked_o),
        .servo_locked_o      (e_servo_locked_o),
        .tl_ewma_credit_o    (e_tl_ewma_credit_o),
        .tidechart_irq_o     (e_tidechart_irq_o)
    );

    // =====================================================================
    // DIE C — the COMPUTE chiplet. Link 0 is the cabled face and is the link
    // SLAVE (role_strap_i_0 = 1); nego_priority low; puf_seed distinct.
    //
    // Link 1 is NOT cabled: its RX pads are held at 0 and its straps are set so
    // it cannot win a negotiation. A test asserts link_active_o_1 stays low.
    //
    // NOTE the boundary difference that defines this whole exercise: there is no
    // `eth_ss_0_*` here, and no other AHB/APB ingress. Everything a test can do
    // to this die, it does through these strap pins.
    // =====================================================================
    nanosoc_compute_chiplet #(.NUM_PHY_LANES(NUM_PHY_LANES)) u_dieC (
        .sys_fclk            (sys_fclk),
        .sys_sysresetn       (c_sysresetn),
        .sys_poresetn        (c_sys_poresetn),
        .sys_hclk            (c_sys_hclk),
        .sys_hresetn         (c_sys_hresetn),
        .sys_scanenable      (1'b0),
        .sys_testmode        (1'b0),
        .sys_sysresetreq     (1'b0),

        .qspi_sclk           (c_qspi_sclk),
        .qspi_csn            (c_qspi_csn),
        .qspi_io_o           (c_qspi_io_o),
        .qspi_io_i           (c_qspi_io_i),
        .qspi_io_e           (c_qspi_io_e),

        .uart_rxd            (1'b1),
        .uart_txd            (c_uart_txd),

        .phc_pps_out         (c_phc_pps_out),

        .dap_swclktck        (1'b0),
        .dap_swditms         (1'b1),
        .dap_swdo            (c_dap_swdo),
        .dap_swdoen          (c_dap_swdoen),
        .dap_tdi             (1'b0),
        .dap_tdo             (c_dap_tdo),
        .dap_ntdoen          (c_dap_ntdoen),
        .dap_ntrst           (1'b1),
        .dap_npotrst         (c_sysresetn),
        .dap_swj_enable      (1'b1),

        // --- DFT (shared across both links; chain is internal) ---
        .scan_mode           (1'b0),
        .scan_asyncrst_ctrl  (1'b0),
        .scan_clk            (1'b0),
        .scan_shift          (1'b0),
        .scan_in             (1'b0),
        .scan_out            (c_scan_out),

        // ---------------- LINK 0: the cabled face, to die E ----------------
        .pad_clk_tx_0        (c_pad_clk_tx_0),
        .pad_tx_0            (c_pad_tx_0),
        .pad_clk_rx_0        (e_pad_clk_tx_skid & e_pad_en),
        .pad_rx_0            (e_pad_tx_skid & {NUM_PHY_LANES{e_pad_en}}),
        .user_ref_clk_0      (ref_clk),
        .idelay_ref_clk_0    (1'b0),

        .i2c_scl_i_0         (i2c_scl),
        .i2c_scl_o_0         (c_i2c_scl_o_0),
        .i2c_scl_t_0         (c_i2c_scl_t_0),
        .i2c_sda_i_0         (i2c_sda),
        .i2c_sda_o_0         (c_i2c_sda_o_0),
        .i2c_sda_t_0         (c_i2c_sda_t_0),

        .role_strap_i_0      (1'b1),          // compute die follows (slave)
        .mask_hs_bypass_i_0  (1'b1),
        .apb_debug_unlock_i_0(1'b1),
        .nego_priority_i_0   (16'h7FFF),
        .puf_seed_0          (16'h5A5A),
        .puf_ready_0         (1'b1),

        .link_active_o_0     (c_link_active_o_0),
        .d2d_reset_o_0       (c_d2d_reset_o_0),
        .role_is_master_o_0  (c_role_is_master_o_0),
        .role_locked_o_0     (c_role_locked_o_0),
        .servo_locked_o_0    (c_servo_locked_o_0),
        .tl_ewma_credit_o_0  (c_tl_ewma_credit_o_0),

        // ------------- LINK 1: NOT cabled. Must stay down. -----------------
        // RX held at 0 (no far die), reference clock still supplied so the
        // block is not X-poisoned, straps set so it cannot train or win a
        // negotiation. `apb_debug_unlock_i_1` low keeps the SW role-lock path
        // shut on the uncabled link.
        .pad_clk_tx_1        (c_pad_clk_tx_1),
        .pad_tx_1            (c_pad_tx_1),
        .pad_clk_rx_1        (1'b0),
        .pad_rx_1            ({NUM_PHY_LANES{1'b0}}),
        .user_ref_clk_1      (ref_clk),
        .idelay_ref_clk_1    (1'b0),

        .i2c_scl_i_1         (1'b1),          // idle-high open-drain, no peer
        .i2c_scl_o_1         (c_i2c_scl_o_1),
        .i2c_scl_t_1         (c_i2c_scl_t_1),
        .i2c_sda_i_1         (1'b1),
        .i2c_sda_o_1         (c_i2c_sda_o_1),
        .i2c_sda_t_1         (c_i2c_sda_t_1),

        .role_strap_i_1      (1'b1),
        .mask_hs_bypass_i_1  (1'b0),
        .apb_debug_unlock_i_1(1'b0),
        .nego_priority_i_1   (16'h0000),
        .puf_seed_1          (16'h0000),
        .puf_ready_1         (1'b0),

        .link_active_o_1     (c_link_active_o_1),
        .d2d_reset_o_1       (c_d2d_reset_o_1),
        .role_is_master_o_1  (c_role_is_master_o_1),
        .role_locked_o_1     (c_role_locked_o_1),
        .servo_locked_o_1    (c_servo_locked_o_1),
        .tl_ewma_credit_o_1  (c_tl_ewma_credit_o_1),

        .tidechart_irq_o     (c_tidechart_irq_o)
    );

    // =====================================================================
    // Autonomous link negotiation — set as a PARAMETER, at time 0.
    //
    // `NEGO_CFG_RESET` is the POR value of TideLink's `nego_cfg_reg`
    // (tidelink_top.sv:123). Both chiplet tops instantiate `tidelink_top`
    // WITHOUT overriding it, so it defaults to 7'h00 => nego_en = 0 => the
    // autoneg FSM parks in ST_BYPASS => `role_lock_reg` is never set => the
    // Wlink is held in reset forever (axi_chiplet_controller.sv:2832) =>
    // `link_active` can never assert. The homogeneous pair works around that
    // with APB writes to ROLE_CFG on BOTH dies — a route that does not exist
    // here, because the compute chiplet has no bus (see the header).
    //
    // 7'h61 = nego_en | nego_force_lock | mask_hs_auto_en. It is the value
    // TideLink's own ASIC DFT wrapper already defaults to
    // (tidelink/src/rtl/asic/tidelink_dft_wrapper.sv:137) — i.e. the posture
    // these chiplets are intended to tape out in — and the value under which
    // tidelink/cocotb/tidelink_top_pair/test_zeropoke_por.py proves a pair
    // reaches bilateral cal = S_DONE and fcsm = 4 with ZERO register writes.
    //
    // WHY defparam AND NOT A COCOTB REGISTER WRITE. Poking `nego_cfg_reg` from
    // the test after reset gets the dies as far as role-lock and cal_done, but
    // the FCSM then sticks at state 1 on both: by the time the write lands the
    // negotiation and link-enable sequencing have already been decided from the
    // POR value. The parameter has to be right AT TIME 0, which is exactly what
    // the upstream zero-poke test does via `+define+TB_TOP_NEGO_CFG_RESET`.
    // Doing it here keeps both chiplet RTL files untouched.
    //
    // This is a testbench-side configuration of a real design parameter, not a
    // stub: it changes no logic and weakens no assertion. That both shipping
    // tops currently hard-code the firmware-dependent value is recorded as
    // finding F3 in docs/SIM_PLAN.md, where the one-line RTL fix is spelled out.
    // =====================================================================
`ifndef HET_PAIR_NO_AUTONEG_PARAM
    defparam u_dieE.u_tidelink.NEGO_CFG_RESET   = 7'h61;
    defparam u_dieC.u_tidelink_0.NEGO_CFG_RESET = 7'h61;
    // Link 1 is uncabled: leave it at the shipping default so it cannot train.
`endif

    // =====================================================================
    // Per-die QSPI flash models (unprogrammed).
    // =====================================================================
    sst26vf064b u_flash_e (.SCK(e_qspi_sclk), .SIO(e_spi_io), .CEb(e_qspi_csn));
    defparam u_flash_e.I0.Tbe  = 1_000;
    defparam u_flash_e.I0.Tse  = 1_000;
    defparam u_flash_e.I0.Tsce = 1_000;
    defparam u_flash_e.I0.Tpp  = 1_000;
    defparam u_flash_e.I0.Tws  = 1_000;

    sst26vf064b u_flash_c (.SCK(c_qspi_sclk), .SIO(c_spi_io), .CEb(c_qspi_csn));
    defparam u_flash_c.I0.Tbe  = 1_000;
    defparam u_flash_c.I0.Tse  = 1_000;
    defparam u_flash_c.I0.Tsce = 1_000;
    defparam u_flash_c.I0.Tpp  = 1_000;
    defparam u_flash_c.I0.Tws  = 1_000;

`ifdef DUMP_FSDB
    initial begin
        $dumpfile("waves.vcd");
        $dumpvars(0, tb_het_pair);
    end
`endif

endmodule
