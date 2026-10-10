// 一个 8 位累加器。故意埋了一个 bug：第 3 位被写成了 ~counter[2]，
// 导致累加到 4 的时候第 3 位开始出错。
// 这类「单个位接错」的 bug 在文本报错里几乎看不出来，但在逐拍差分里一眼现形。
module counter8 (
    input  wire       clk,
    input  wire       rst_n,
    input  wire       en,
    output reg  [7:0] count,
    output wire       carry
);
    reg [7:0] next;

    always @(*) begin
        next = count + 8'd1;
        // BUG: 这一位本该是 next[3]，却写成了取反的 count[2]
        next[3] = ~count[2];
    end

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n)
            count <= 8'd0;
        else if (en)
            count <= next;
    end

    assign carry = (count == 8'hFF);

endmodule
