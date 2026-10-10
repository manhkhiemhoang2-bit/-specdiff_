// 正确版本的 8 位累加器：与 counter8.v 唯一的差别是修掉了那一行 bug。
// 用它跑一遍，wavediff 应当报「全部一致」——
// 这证明工具不是「永远报错」，而是真的在比对。
module counter8_fixed (
    input  wire       clk,
    input  wire       rst_n,
    input  wire       en,
    output reg  [7:0] count,
    output wire       carry
);
    reg [7:0] next;

    always @(*) begin
        next = count + 8'd1;      // 正确写法
    end

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n)
            count <= 8'd0;
        else if (en)
            count <= next;
    end

    assign carry = (count == 8'hFF);

endmodule
