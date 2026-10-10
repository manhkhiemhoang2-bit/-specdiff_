// 测试平台：驱动时钟、复位、使能，把 8 位计数器的值跑 40 拍。
// 注意：这里有输出，但**不判断对错** —— 它只负责产生波形。
// 判断对错是 wavediff 的活，这正是分工的意义。
`timescale 1ns/1ps

module tb_counter8;
    reg        clk;
    reg        rst_n;
    reg        en;
    wire [7:0] count;
    wire       carry;

    counter8 dut (
        .clk   (clk),
        .rst_n (rst_n),
        .en    (en),
        .count (count),
        .carry (carry)
    );

    // 10ns 周期
    initial clk = 0;
    always #5 clk = ~clk;

    integer i;
    initial begin
        $dumpfile("counter8.vcd");
        $dumpvars(0, tb_counter8);

        rst_n = 0;
        en    = 0;
        #23;              // 复位保持一段（非整周期，模拟真实情况）
        rst_n = 1;
        #7;               // 对齐到时钟边沿之后
        en    = 1;

        for (i = 0; i < 40; i = i + 1) begin
            @(posedge clk);
            $display("cycle %0d  count=%0d  carry=%b", i, count, carry);
        end

        $finish;
    end
endmodule
