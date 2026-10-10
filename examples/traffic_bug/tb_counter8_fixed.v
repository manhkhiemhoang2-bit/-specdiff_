// 正确版本的测试平台，与 tb_counter8.v 完全一致，只换成正确模块。
// 用于验证「修好之后 wavediff 确实报通过」。
`timescale 1ns/1ps

module tb_counter8_fixed;
    reg        clk;
    reg        rst_n;
    reg        en;
    wire [7:0] count;
    wire       carry;

    counter8_fixed dut (
        .clk   (clk),
        .rst_n (rst_n),
        .en    (en),
        .count (count),
        .carry (carry)
    );

    initial clk = 0;
    always #5 clk = ~clk;

    integer i;
    initial begin
        $dumpfile("counter8_fixed.vcd");
        $dumpvars(0, tb_counter8_fixed);

        rst_n = 0;
        en    = 0;
        #23;
        rst_n = 1;
        #7;
        en    = 1;

        for (i = 0; i < 40; i = i + 1) begin
            @(posedge clk);
            $display("cycle %0d  count=%0d  carry=%b", i, count, carry);
        end

        $finish;
    end
endmodule
