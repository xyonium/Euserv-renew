#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
eu.py 续期校验逻辑的离线测试（无网络，Fake 会话重放各步骤响应）。
运行: python3 test_renew_checks.py

核心回归场景（2026-09-01/09-02 真实事故）：
  登录/PIN/token 全部成功，但 extend_contract_term 被 EUserv 拒绝
  （真实原因：合同为自动续期，手动续期被拒绝）。旧版 renew() 无条件
  return True；新版必须区分 failed / auto_renew，绝不错报成功。
"""
import json
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
<tr><td>Contract begin: 2026-08-05</td></tr>
<tr><td>End of contract period: 2026-09-05</td></tr>
</table></body></html>
"""

DETAILS_AFTER = DETAILS_BEFORE.replace("2026-09-05", "2026-10-05")

# 真实抓取的自动续期合同详情页关键内容（2026-09-02 运行）
DETAILS_AUTO_RENEW = """
<html><body><table>
<tr><td>End of contract period: 2026-10-02</td></tr>
<tr><td>Latest termination date: 2026-10-01 to not extend the contract
automatically until the 2026-11-02</td></tr>
</table></body></html>
"""

# 真实抓取的确认对话框结构（{"html": {"value": "<form>..."}}）
CONFIRM_REAL = json.dumps({"html": {"value":
    '<form id="kc2_customer_contract_details_extend_contract_form" action="/index.iphp" '
    'method="post"><input type="hidden" name="sess_id" value="abc">'
    '<input type="hidden" name="ord_id" value="488048">'
    '<input type="hidden" name="subaction" value="kc2_customer_contract_details_extend_contract_term">'
    '<input type="hidden" name="token" value="tok123"></form>'
    '<div>The selected contract 488048 will be extended until 2026-11-02.</div>'}})

CONFIRM_EXTRA_FIELD = """
<div id="kc2_customer_contract_details_extend_contract_confirmation_dialog">
<form><input type="hidden" name="confirm_extension" value="1">
<p>Do you really want to extend the contract?</p></form></div>
"""

CONFIRM_CAPTCHA = '<div>Please solve the captcha: <img src="securimage_show.php"></div>'

EXTEND_OK = '{"rs": "success", "token": {"value": "tok-used"}}'
EXTEND_ERR = '{"rs": "error", "msg": {"0": "The security token is invalid"}}'

# 真实抓取的自动续期拒绝页面关键结构（红字错误行）
EXTEND_AUTO_RENEW_PAGE = """
<html><body>
<table class="kc2_content_table">
<tr><td colspan="2" class="verdana14px-rot-b">Error: Manual contract extension
is not possible because the contract is extended automatically.</td></tr>
</table></body></html>
"""

# 主界面服务级停用警告（用户在控制台实际看到的文字，2026-09-02）
OVERVIEW_WITH_DEACTIVATION_WARNING = """
<html><body>
<div id="kc2_order_customer_orders_tab_content_1">
<table class="kc2_order_table kc2_content_table">
<tr>
  <td class="td-z1-sp1-kc">488048</td>
  <td class="td-z1-sp2-kc"><div class="kc2_order_action_container">Extend contract</div></td>
</tr>
</table></div>
<div class="warning">The service will be automatically deactivated on 2026-09-09
if it is not extended manually.</div>
</body></html>
"""

OVERVIEW_CLEAN = """
<html><body>
<div id="kc2_order_customer_orders_tab_content_1">
<table class="kc2_order_table kc2_content_table">
<tr>
  <td class="td-z1-sp1-kc">488048</td>
  <td class="td-z1-sp2-kc"><div class="kc2_order_action_container">
  Contract extension possible from 2026-10-01</div></td>
</tr>
</table></div>
</body></html>
"""


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

    def __init__(self, details_html, action_text, extend_resp, confirm_resp=CONFIRM_REAL):
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


def test_extend_error_returns_failed():
    """extend_contract_term 明确返回错误 → failed（旧版此处返回 True）。"""
    s = FakeSession(DETAILS_BEFORE, "Extend contract", EXTEND_ERR)
    assert run_renew(s) == "failed"
    print("✓ test_extend_error_returns_failed")


def test_extend_success_and_date_advances():
    """extend 成功 + 轮询发现到期日后移 → success。"""
    s = FakeSession(DETAILS_BEFORE, "Extend contract", EXTEND_OK)
    orig_post = s.post
    state = {"n": 0}

    def post_advance(url, headers=None, data=None, timeout=None):
        if data.get("subaction") == "choose_order":
            state["n"] += 1
            if state["n"] > 1:
                return FakeResponse(DETAILS_AFTER)
        return orig_post(url, headers=headers, data=data, timeout=timeout)

    s.post = post_advance
    assert run_renew(s, timeout=60) == "success"
    print("✓ test_extend_success_and_date_advances")


def test_extend_success_but_never_applies():
    """【2026-09-01 事故复现】extend 响应成功但面板到期日不变、按钮仍在 → failed。"""
    s = FakeSession(DETAILS_BEFORE, '<a>Extend contract</a>', EXTEND_OK)
    assert run_renew(s, timeout=0) == "failed"
    print("✓ test_extend_success_but_never_applies (事故复现)")


def test_auto_renew_marker_does_not_skip_flow():
    """【用户指令回归】详情页含"extended automatically"也绝不跳过续期流程。

    合同层自动滚动 ≠ 服务无需手动续期（主界面停用警告才是服务级事实）。
    FakeSession 中 extend 返回 OK 但到期日不后移 → failed；且必须触发 PIN 邮件。
    """
    s = FakeSession(DETAILS_AUTO_RENEW, "Extend contract", EXTEND_OK)
    assert run_renew(s, timeout=0) == "failed"
    pin_posts = [p for p in s.posts
                 if p.get("subaction") == "show_kc2_security_password_dialog"]
    assert pin_posts, "看到 automatically 字样也绝不能跳过手动续期流程"
    extend_posts = [p for p in s.posts
                    if p.get("subaction") == "kc2_customer_contract_details_extend_contract_term"]
    assert extend_posts, "续期请求必须照常发出"
    print("✓ test_auto_renew_marker_does_not_skip_flow (绝不跳过)")


def test_auto_renew_detected_at_step7():
    """详情页无标记但执行续期时被拒（自动续期红字错误）→ auto_renew。"""
    s = FakeSession(DETAILS_BEFORE, "Extend contract", EXTEND_AUTO_RENEW_PAGE)
    assert run_renew(s) == "auto_renew"
    print("✓ test_auto_renew_detected_at_step7")


def test_confirmation_dialog_captcha():
    """确认对话框出现验证码 → failed 而不是盲续。"""
    s = FakeSession(DETAILS_BEFORE, "Extend contract", EXTEND_OK,
                    confirm_resp=CONFIRM_CAPTCHA)
    assert run_renew(s) == "failed"
    print("✓ test_confirmation_dialog_captcha")


def test_hidden_fields_forwarded():
    """确认对话框里的隐藏字段必须被转发到 extend_contract_term 请求。"""
    s = FakeSession(DETAILS_BEFORE, "Extend contract", EXTEND_ERR,
                    confirm_resp=CONFIRM_EXTRA_FIELD)
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
        (EXTEND_AUTO_RENEW_PAGE, "auto_renew"),
    ]
    for text, want in cases:
        got, _ = eu.classify_extend_response(FakeResponse(text))
        assert got == want, (text[:60], got, want)
    # 红字错误行文本应被提取
    _, detail = eu.classify_extend_response(FakeResponse(EXTEND_AUTO_RENEW_PAGE))
    assert "not possible" in detail
    print("✓ test_extend_response_classification")


def test_pick_contract_end_date():
    assert str(eu.pick_contract_end_date(DETAILS_BEFORE)) == "2026-09-05"
    # 自动续期详情页：当前期末 2026-10-02（"until 2026-11-02" 是自动续期目标，不算）
    assert str(eu.pick_contract_end_date(DETAILS_AUTO_RENEW)) == "2026-10-02"
    assert eu.pick_contract_end_date("<html>no dates here</html>") is None
    print("✓ test_pick_contract_end_date (含 ISO 格式)")


def test_confirmation_dialog_validation():
    # 真实结构 {"html": {"value": ...}}：解包成功，4 个标准字段不重复转发
    ok, extra = eu.validate_confirmation_dialog(FakeResponse(CONFIRM_REAL), "488048")
    assert ok and extra == {}, (ok, extra)
    # 含额外隐藏字段
    ok, extra = eu.validate_confirmation_dialog(FakeResponse(CONFIRM_EXTRA_FIELD), "488048")
    assert ok and extra.get("confirm_extension") == "1"
    # JSON 错误
    ok, _ = eu.validate_confirmation_dialog(
        FakeResponse('{"rs": "error", "msg": "bad token"}'), "488048")
    assert not ok
    # JSON 包裹含验证码
    wrapped = json.dumps({"html": {"value": CONFIRM_CAPTCHA}})
    ok, _ = eu.validate_confirmation_dialog(FakeResponse(wrapped), "488048")
    assert not ok
    print("✓ test_confirmation_dialog_validation")


def test_mask_payload():
    out = eu._mask_payload({"auth": "123456", "sess_id": "abc", "token": "tok"})
    assert "123456" not in out and "tok\"" not in out and "abc" in out
    print("✓ test_mask_payload")


# 【2026-09-02 真实事故结构】ESS 存储试用合同占了 tab_1，
# 真正的 VS2 VPS 合同在 tab_2 —— 旧选择器只看 tab_1，漏掉了 VPS。
MULTI_TAB_OVERVIEW = """
<html><body>
<div id="kc2_order_customer_orders_tab_content_1">
<table class="kc2_order_table kc2_content_table">
<tr>
  <td class="td-z1-sp1-kc">488048</td>
  <td class="td-z1-sp2-kc"><div class="kc2_order_action_container">Extend contract</div></td>
  <td>ESS public alphatest</td>
</tr>
</table></div>
<div id="kc2_order_customer_orders_tab_content_2">
<table class="kc2_order_table kc2_content_table">
<tr>
  <td class="td-z1-sp1-kc">481668</td>
  <td class="td-z1-sp2-kc"><div class="kc2_order_action_container">Extend contract</div></td>
  <td>VS2-free</td>
</tr>
</table></div>
</body></html>
"""


class CheckFakeSession:
    """供 check()/get_servers() 使用：GET 固定返回给定概览页。"""

    def __init__(self, overview_html):
        self.overview_html = overview_html

    def get(self, url, headers=None, timeout=None):
        return FakeResponse(self.overview_html)


def test_get_servers_scans_all_order_tabs():
    """【2026-09 事故回归】合同枚举必须覆盖所有订单标签页，
    不能因 ESS 试用合同占据 tab_1 而漏掉其它标签页里的 VPS。"""
    d = eu.get_servers("sess123", CheckFakeSession(MULTI_TAB_OVERVIEW))
    assert "481668" in d, "VPS 合同 481668 在 tab_2，绝不能被漏掉"
    assert "488048" in d, "ESS 合同 488048 也应被枚举"
    assert d["481668"] is True and d["488048"] is True
    print("✓ test_get_servers_scans_all_order_tabs (事故回归)")


def test_get_deactivation_warnings():
    """主界面"will be automatically deactivated on ... if it is not extended
    manually"必须被抓出——这是服务是否需要手动续期的事实信号。"""
    s = CheckFakeSession(OVERVIEW_WITH_DEACTIVATION_WARNING)
    warnings = eu.get_deactivation_warnings(s, "sess123")
    assert warnings, "停用警告必须被检测到"
    assert any("2026-09-09" in w for w in warnings), warnings
    assert eu.get_deactivation_warnings(
        CheckFakeSession(OVERVIEW_CLEAN), "sess123") == []
    print("✓ test_get_deactivation_warnings")


def test_check_deactivation_warning_overrides_auto_skip():
    """【核心安全不变量】即使合同被 renew() 判定为 auto_renew 并加入 skip，
    只要面板停用警告还在，check() 就必须把它报出来（主流程据此告警退出非零）。"""
    failed, warnings = eu.check(
        "sess123", CheckFakeSession(OVERVIEW_WITH_DEACTIVATION_WARNING),
        skip=("488048",))
    assert failed == [], "skip 的合同不应出现在按钮终检失败列表"
    assert warnings, "skip 不能盖过停用警告"
    failed2, warnings2 = eu.check(
        "sess123", CheckFakeSession(OVERVIEW_CLEAN), skip=("488048",))
    assert failed2 == [] and warnings2 == []
    print("✓ test_check_deactivation_warning_overrides_auto_skip")


if __name__ == "__main__":
    test_extend_error_returns_failed()
    test_extend_success_and_date_advances()
    test_extend_success_but_never_applies()
    test_auto_renew_marker_does_not_skip_flow()
    test_auto_renew_detected_at_step7()
    test_confirmation_dialog_captcha()
    test_hidden_fields_forwarded()
    test_extend_response_classification()
    test_pick_contract_end_date()
    test_confirmation_dialog_validation()
    test_mask_payload()
    test_get_servers_scans_all_order_tabs()
    test_get_deactivation_warnings()
    test_check_deactivation_warning_overrides_auto_skip()
    print("\n全部测试通过 ✅")
