// ============================================================================
// tb_d2d_decode — L0-SIM-15. Both window placements of chiplet_d2d_decode,
// side by side, in one simulation.
//
// WHY BOTH IN ONE TB
// ------------------
// The long-open question is not "does the decoder work" but "does the COMPUTE
// placement decode where everyone assumes it does". Instantiating the eth
// placement alongside it makes that a COMPARISON rather than an assertion
// against a remembered number: the eth map is the one proven on hardware, so
// if the compute instance disagrees in shape, the test says which of the two
// moved.
//
// Both instances are the SAME RTL — the G4 parameterisation is the whole point.
// A wrapper-per-window would test the wrapper, not the decoder.
//
// Copyright 2026, SoC Labs (www.soclabs.org)
// ============================================================================
`timescale 1ns/1ps

module tb_d2d_decode;

    reg         hclk = 1'b0;
    reg         hresetn = 1'b0;
    always #5 hclk = ~hclk;

    reg  [31:0] haddr = 32'h0;
    reg   [1:0] htrans = 2'b00;
    reg         link_active_i = 1'b1;

    // Slave-side responses. Tied to a benign OKAY/ready so the decoder's own
    // default responder is the only thing that can raise hresp — otherwise a
    // slave could mask the very fault this test looks for.
    localparam [31:0] RD = 32'hA5A5_A5A5;

    // ---- eth placement (the hardware-proven map: config 0x2E, peer 0x2F) ----
    wire e_hsel_tx, e_hsel_fifo, e_hsel_ptp, e_hsel_tlapb, e_hsel_tcapb, e_hsel_peer;
    wire e_hready, e_hresp;
    wire [31:0] e_hrdata;

    chiplet_d2d_decode #(.WINDOW_BASE(32'h2E00_0000)) u_eth (
        .hclk(hclk), .hresetn(hresetn), .haddr(haddr), .htrans(htrans),
        .link_active_i(link_active_i),
        .hrdata(e_hrdata), .hready(e_hready), .hresp(e_hresp),
        .hsel_tx(e_hsel_tx), .hsel_fifo(e_hsel_fifo), .hsel_ptp(e_hsel_ptp),
        .hsel_tlapb(e_hsel_tlapb), .hsel_tcapb(e_hsel_tcapb), .hsel_peer(e_hsel_peer),
        .dph_peer(),
        .hrdata_tx(RD),    .hreadyout_tx(1'b1),    .hresp_tx(1'b0),
        .hrdata_fifo(RD),  .hreadyout_fifo(1'b1),  .hresp_fifo(1'b0),
        .hrdata_ptp(RD),   .hreadyout_ptp(1'b1),   .hresp_ptp(1'b0),
        .hrdata_tlapb(RD), .hreadyout_tlapb(1'b1), .hresp_tlapb(1'b0),
        .hrdata_tcapb(RD), .hreadyout_tcapb(1'b1), .hresp_tcapb(1'b0),
        .hrdata_peer(RD),  .hreadyout_peer(1'b1),  .hresp_peer(1'b0)
    );

    // ---- compute link 0 (the map under question: config 0x40, peer 0x41) ----
    wire c_hsel_tx, c_hsel_fifo, c_hsel_ptp, c_hsel_tlapb, c_hsel_tcapb, c_hsel_peer;
    wire c_hready, c_hresp;
    wire [31:0] c_hrdata;

    chiplet_d2d_decode #(.WINDOW_BASE(32'h4000_0000)) u_cmp (
        .hclk(hclk), .hresetn(hresetn), .haddr(haddr), .htrans(htrans),
        .link_active_i(link_active_i),
        .hrdata(c_hrdata), .hready(c_hready), .hresp(c_hresp),
        .hsel_tx(c_hsel_tx), .hsel_fifo(c_hsel_fifo), .hsel_ptp(c_hsel_ptp),
        .hsel_tlapb(c_hsel_tlapb), .hsel_tcapb(c_hsel_tcapb), .hsel_peer(c_hsel_peer),
        .dph_peer(),
        .hrdata_tx(RD),    .hreadyout_tx(1'b1),    .hresp_tx(1'b0),
        .hrdata_fifo(RD),  .hreadyout_fifo(1'b1),  .hresp_fifo(1'b0),
        .hrdata_ptp(RD),   .hreadyout_ptp(1'b1),   .hresp_ptp(1'b0),
        .hrdata_tlapb(RD), .hreadyout_tlapb(1'b1), .hresp_tlapb(1'b0),
        .hrdata_tcapb(RD), .hreadyout_tcapb(1'b1), .hresp_tcapb(1'b0),
        .hrdata_peer(RD),  .hreadyout_peer(1'b1),  .hresp_peer(1'b0)
    );

    // ---- compute link 1 — the second window must NOT alias onto link 0 ----
    wire l1_hsel_peer, l1_hsel_tlapb, l1_hsel_tx;
    chiplet_d2d_decode #(.WINDOW_BASE(32'h6000_0000)) u_cmp1 (
        .hclk(hclk), .hresetn(hresetn), .haddr(haddr), .htrans(htrans),
        .link_active_i(link_active_i),
        .hrdata(), .hready(), .hresp(),
        .hsel_tx(l1_hsel_tx), .hsel_fifo(), .hsel_ptp(),
        .hsel_tlapb(l1_hsel_tlapb), .hsel_tcapb(), .hsel_peer(l1_hsel_peer),
        .dph_peer(),
        .hrdata_tx(RD),    .hreadyout_tx(1'b1),    .hresp_tx(1'b0),
        .hrdata_fifo(RD),  .hreadyout_fifo(1'b1),  .hresp_fifo(1'b0),
        .hrdata_ptp(RD),   .hreadyout_ptp(1'b1),   .hresp_ptp(1'b0),
        .hrdata_tlapb(RD), .hreadyout_tlapb(1'b1), .hresp_tlapb(1'b0),
        .hrdata_tcapb(RD), .hreadyout_tcapb(1'b1), .hresp_tcapb(1'b0),
        .hrdata_peer(RD),  .hreadyout_peer(1'b1),  .hresp_peer(1'b0)
    );

    initial begin
        hresetn = 1'b0;
        repeat (4) @(posedge hclk);
        hresetn = 1'b1;
    end
endmodule
