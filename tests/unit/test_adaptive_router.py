import pytest

from app.agents.nodes.intent import rule_based_intent
from app.core.action_requests import explicit_write_request
from app.core.enums import IntentLabel, RetrievalStrategy
from app.retrieval.adaptive import rule_based_complexity
from app.schemas.agent import IntentResult


def test_greeting_goes_direct() -> None:
    intent = rule_based_intent("你好")
    assert intent.label == IntentLabel.GREETING
    routed = rule_based_complexity("你好", intent)
    assert routed.strategy == RetrievalStrategy.DIRECT


def test_order_goes_agent() -> None:
    intent = IntentResult(label=IntentLabel.ORDER_INQUIRY, confidence=0.9, prompt_version="t")
    routed = rule_based_complexity("查一下订单 ORD10001", intent)
    assert routed.strategy == RetrievalStrategy.AGENT


def test_policy_multi_hop() -> None:
    intent = IntentResult(label=IntentLabel.FAQ_POLICY, confidence=0.8, prompt_version="t")
    query = "定制马克杯过了7天但是质量问题还能退吗"
    routed = rule_based_complexity(query, intent)
    assert routed.strategy in {RetrievalStrategy.MULTI, RetrievalStrategy.SINGLE}


def test_general_return_policy_does_not_request_order_tools() -> None:
    intent = IntentResult(label=IntentLabel.RETURN_EXCHANGE, confidence=0.8, prompt_version="t")
    routed = rule_based_complexity("七天无理由退货的规则是什么？", intent)
    assert routed.strategy == RetrievalStrategy.SINGLE


def test_specific_order_return_uses_tools() -> None:
    intent = IntentResult(label=IntentLabel.RETURN_EXCHANGE, confidence=0.8, prompt_version="t")
    routed = rule_based_complexity("订单 ORD10002 还能退货吗？", intent)
    assert routed.strategy == RetrievalStrategy.AGENT


@pytest.mark.parametrize("query", ["我要把马克杯退掉，太丑了", "帮我把马克杯退回去"])
def test_colloquial_return_uses_tools_to_request_order(query) -> None:
    intent = rule_based_intent(query)
    assert intent.label == IntentLabel.RETURN_EXCHANGE
    assert rule_based_complexity(query, intent).strategy == RetrievalStrategy.AGENT
    assert explicit_write_request(query, "create_return_request")


@pytest.mark.parametrize("query", ["马克杯能退掉吗？", "马克杯可以退掉吗？", "退货需要什么条件？"])
def test_return_questions_do_not_authorize_writes(query) -> None:
    assert not explicit_write_request(query, "create_return_request")
