"""Shared checks for explicit business writes versus policy questions."""


def explicit_write_request(query: str, tool_name: str) -> bool:
    if "不取消" in query:
        return False
    if any(
        cue in query
        for cue in (
            "不要",
            "别",
            "不用",
            "不退了",
            "先不",
            "取消申请",
            "取消退货申请",
            "取消退款申请",
        )
    ):
        return False
    if any(
        cue in query
        for cue in (
            "规则",
            "政策",
            "条件",
            "怎么",
            "能不能",
            "是否",
            "能否",
            "可不可以",
            "可以吗",
            "能退吗",
            "能取消吗",
            "可以退",
            "能退掉",
            "能退回",
        )
    ):
        return False
    cues = {
        "cancel_order": ("取消", "不想要了"),
        "create_refund_request": ("申请退款", "帮我退款", "我要退款"),
        "create_return_request": ("申请退货", "帮我退货", "我要退货", "提交退货", "退掉", "退回去"),
    }
    return any(cue in query for cue in cues.get(tool_name, ()))
