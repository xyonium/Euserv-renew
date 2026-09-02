#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
eu.py 续期校验逻辑的离线测试（无网络，Fake 会话重放各步骤响应）。
运行: python3 test_renew_checks.py

核心回归场景（2026-09-01 真实事故）：
  登录/PIN/token 全部成功，但 extend_contract_term 被 EUserv 拒绝或未生效。
  旧版 renew() 无条件 return True；新版必须返回 False。
"""
import json
import os
import sys
import tempfile
import types

# eu.py 顶层会 `from gmail_api import *`，本地没有 google 依赖，打桩绕过
sys.modules.setdefault("gmail_api", types.ModuleType("gmail_api"))

import eu

# 测试环境设置：快速轮询、独立 dump 目录
eu.VERIFY_INTERVAL = 0
eu.DEBUG_DIR = tempfile.mkdtemp(prefix="eu_test_dumps_")
eu.userId = "test@example.com"
eu.wait_for_email = lambda request_time: "123456"  # 不碰真实邮箱


class FakeResponse:
    def __init__(self, text, status=200, ctype="text/html"):
        self.text = text
        self.status_code = status
        self.headers = {"Content-Type": ctype}
        self.content = text.encode()

    def json(self):
        return json.loads(self.text)

    def raise_for_status(self):
        if self.status_code >= 400:
            raise Exception("HTTP %s" % self.status_code)


DETAILS_BEFORE = """
<html><body><table>
<tr><td>Contract begin:</td><td>05.08.2026</td></tr>
<tr><td>Contract end:</td><td>05.09.2026</td></tr>
<tr><td>Cancellation possible at:</td><td>05.09.2026</td></tr>
</table></body></html>
"""

DETAILS_AFTER = DETAILS_BEFORE.replace("05.09.2026", "05.10.2026")

CONFIRM_OK = """
<div id="kc2_customer_contract_details_extend_contract_confirmation_dialog">
<form><input type="hidden" name="confirm_extension" value="1">
<p>Do you really want to extend the contract?</p></form></div>
"""

CONFIRM_CAPTCHA = '<div>Please solve the captcha: <img src="securimage_show.php"></div>'

EXTEND_OK = '{"rs": "success", "token": {"value": "tok-used"}}'
EXTEND_ERR = '{"rs": "error", "msg": {"0": "The security token is invalid"}}'


def server_list_html(action_text):
    return """
    <div id="kc2_order_customer_orders_tab_content_1">
    <table class="kc2_order_table kc2_content_table">
    <tr>
      <td class="td-z1-sp1-kc">488048</td>
      <td class="td-z1-sp2-kc"><div class="kc2_order_action_container">%s</div></td>
    </tr>
    </table></div>
    """ % action_text


class FakeSession:
    """按 subaction 分发罐装响应；details_html/action_text/extend_resp 可随测试推进变化。"""

    def __init__(self, details_html, action_text, extend_resp, confirm_resp=CONFIRM_OK):
        self.details_html = details_html
        self.action_text = action_text
        self.extend_resp = extend_resp
        self.confirm_resp = confirm_resp
        self.posts = []  # 记录每次 POST 的 payload，验证「点击记录」与字段转发

    def post(self, url, headers=None, data=None, timeout=None):
        self.posts.append(dict(data))
        sub = data.get("subaction", "")
        if sub == "choose_order":
            return FakeResponse(self.details_html)
        if sub == "kc2_customer_contract_details_get_change_plan_dialog":
            return FakeResponse('{"rs": "success"}', ctype="application/json")
        if sub == "show_kc2_security_password_dialog":
            return FakeResponse("PIN sent to *** ... kc2_security_password_dialog_prompt")
        if sub == "kc2_security_password_get_token":
            return FakeResponse('{"rs": "success", "token": {"value": "tok123"}}',
                                ctype="application/json")
        if sub == "kc2_customer_contract_details_get_extend_contract_confirmation_dialog":
            return FakeResponse(self.confirm_resp)
        if sub == "kc2_customer_contract_details_extend_contract_term":
            return FakeResponse(self.extend_resp, ctype="application/json")
        raise AssertionError("unexpected subaction: %s" % sub)

    def get(self, url, headers=None, timeout=None):
        return FakeResponse(server_list_html(self.action_text))


def run_renew(session, timeout=0):
    old = eu.VERIFY_TIMEOUT
    eu.VERIFY_TIMEOUT = timeout
    try:
        return eu.renew("sess123", session, "pw", "488048")
    finally:
        eu.VERIFY_TIMEOUT = old


def test_extend_error_returns_false():
    """extend_contract_term 明确返回错误 → 必须判失败（旧版此处返回 True）。"""
    s = FakeSession(DETAILS_BEFORE, "Extend contract", EXTEND_ERR)
    assert run_renew(s) is False
    print("✓ test_extend_error_returns_false")


def test_extend_success_and_date_advances():
    """extend 成功 + 轮询发现到期日后移 → True。"""
    s = FakeSession(DETAILS_BEFORE, "Extend contract", EXTEND_OK)
    # 第一次 choose_order（before）用旧日期，之后轮询换成新日期
    orig_post = s.post
    state = {"n": 0}

    def post_advance(url, headers=None, data=None, timeout=None):
        if data.get("subaction") == "choose_order":
            state["n"] += 1
            if state["n"] > 1:
                return FakeResponse(DETAILS_AFTER)
        return orig_post(url, headers=headers, data=data, timeout=timeout)

    s.post = post_advance
    s.action_text = '<a>Extend contract</a>'
    assert run_renew(s, timeout=60) is True
    print("✓ test_extend_success_and_date_advances")


def test_extend_success_but_never_applies():
    """【2026-09-01 事故复现】extend 响应成功但面板到期日不变、按钮仍在 → False。"""
    s = FakeSession(DETAILS_BEFORE, '<a>Extend contract</a>', EXTEND_OK)
    assert run_renew(s, timeout=0) is False
    print("✓ test_extend_success_but_never_applies (事故复现)")


def test_confirmation_dialog_captcha():
    """确认对话框出现验证码 → 明确失败而不是盲续。"""
    s = FakeSession(DETAILS_BEFORE, "Extend contract", EXTEND_OK,
                    confirm_resp=CONFIRM_CAPTCHA)
    assert run_renew(s) is False
    print("✓ test_confirmation_dialog_captcha")


def test_hidden_fields_forwarded():
    """确认对话框里的隐藏字段必须被转发到 extend_contract_term 请求。"""
    s = FakeSession(DETAILS_BEFORE, "Extend contract", EXTEND_ERR)  # error 提前结束即可
    run_renew(s)
    extend_posts = [p for p in s.posts
                    if p.get("subaction") == "kc2_customer_contract_details_extend_contract_term"]
    assert extend_posts, "extend POST 未发出"
    assert extend_posts[0].get("confirm_extension") == "1", extend_posts[0]
    print("✓ test_hidden_fields_forwarded")


def test_extend_response_classification():
    cases = [
        ('{"rs": "success"}', "success"),
        ('{"rs": "error", "msg": "token invalid"}', "error"),
        ('<html><body>Fehler: nicht möglich</body></html>', "error"),
        ('<html><body>some neutral page</body></html>', "unknown"),
    ]
    for text, want in cases:
        got, _ = eu.classify_extend_response(FakeResponse(text))
        assert got == want, (text, got, want)
    print("✓ test_extend_response_classification")


def test_pick_contract_end_date():
    d = eu.pick_contract_end_date(DETAILS_BEFORE)
    assert str(d) == "2026-09-05", d
    assert eu.pick_contract_end_date("<html>no dates here</html>") is None
    print("✓ test_pick_contract_end_date")


def test_confirmation_dialog_validation():
    ok, extra = eu.validate_confirmation_dialog(FakeResponse(CONFIRM_OK), "488048")
    assert ok and extra.get("confirm_extension") == "1"
    ok, _ = eu.validate_confirmation_dialog(
        FakeResponse('{"rs": "error", "msg": "bad token"}'), "488048")
    assert not ok
    # JSON 包裹 HTML 的形式
    wrapped = json.dumps({"rs": "success", "html": CONFIRM_OK})
    ok, extra = eu.validate_confirmation_dialog(FakeResponse(wrapped), "488048")
    assert ok and extra.get("confirm_extension") == "1"
    print("✓ test_confirmation_dialog_validation")


def test_mask_payload():
    out = eu._mask_payload({"auth": "123456", "sess_id": "abc", "token": "tok"})
    assert "123456" not in out and "tok\"" not in out and "abc" in out
    print("✓ test_mask_payload")


if __name__ == "__main__":
    test_extend_error_returns_false()
    test_extend_success_and_date_advances()
    test_extend_success_but_never_applies()
    test_confirmation_dialog_captcha()
    test_hidden_fields_forwarded()
    test_extend_response_classification()
    test_pick_contract_end_date()
    test_confirmation_dialog_validation()
    test_mask_payload()
    print("\n全部测试通过 ✅")
