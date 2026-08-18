"""
离线单元测试（不依赖外网）：手机号归一化 / 运营商 / 抽取 / 配对 / 上下文分级 /
拒收名单 / 跨源合并 / 合规过滤。
运行：
  .workbuddy/binaries/python/envs/default/Scripts/python.exe tests_compliance.py
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import scraper
import compliance

PASS, FAIL = 0, 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        print(f"  ✗ {name}")


print("== 手机号归一化 / 校验 ==")
check("plain", scraper.normalize_mobile("13800138000") == "13800138000")
check("+86 space", scraper.normalize_mobile("+86 138 0013 8000") == "13800138000")
check("0086 dash", scraper.normalize_mobile("0086-138-0013-8000") == "13800138000")
check("86 prefix", scraper.normalize_mobile("86 13800138000") == "13800138000")
check("(86) prefix", scraper.normalize_mobile("(86)13800138000") == "13800138000")
check("too short", scraper.normalize_mobile("1380013800") is None)
check("garbage", scraper.normalize_mobile("12345") is None)
check("valid_mobile true", scraper.valid_mobile("13800138000"))
check("valid_mobile false", not scraper.valid_mobile("23800138000"))

print("== 运营商识别 ==")
check("移动 138", scraper.carrier_of("13800138000") == "中国移动")
check("联通 131", scraper.carrier_of("13100000000") == "中国联通")
check("电信 133", scraper.carrier_of("13300000000") == "中国电信")
check("广电 192", scraper.carrier_of("19200000000") == "中国广电")
check("电信 199", scraper.carrier_of("19900000000") == "中国电信")
check("unknown", scraper.carrier_of("12300000000") == "未知")

print("== 上下文分级 ==")
c1 = compliance.classify_context("招标公告 项目部 联系人张三 13800138000", has_name_pair=True)
check("商务场景个人号=medium", c1["context"] == "personal_in_business" and c1["risk"] == "medium")
c2 = compliance.classify_context("公司总机 029-86531300 业务合作", has_name_pair=False)
check("企业业务线=low", c2["context"] == "business_line" and c2["risk"] == "low")
c3 = compliance.classify_context("我的私人手机 13800138000 周末爬山", has_name_pair=True)
check("未知个人=high", c3["context"] == "personal_unknown" and c3["risk"] == "high")

print("== 抽取 + 配对（样例 HTML） ==")
HTML = """
<html><body>
<script>var x=1;</script>
<p>招标公告：项目部联系人 张三 13800138000，座机 029-86531300。</p>
<p>邮箱：limu@cepec.cn 业务合作请致电。</p>
</body></html>
"""
rec = scraper.extract_contacts(HTML, "https://example.com/page")
check("抽到手机号", "13800138000" in rec["mobiles"])
check("抽到座机", "029-86531300" in rec["phones"])
check("抽到邮箱", "limu@cepec.cn" in rec["emails"])
check("姓名+手机配对", any(ct["name"] == "张三" and ct["mobile"] == "13800138000" for ct in rec["contacts"]))
check("配对带运营商", any(ct.get("carrier") == "中国移动" for ct in rec["contacts"]))

print("== 合规过滤 _apply_compliance ==")
# 高风险纯个人号，未开启 include_personal -> 丢弃（真实场景下该文本不会抽出姓名/角色）
def make_hi():
    return {"source": "https://x.com/a", "emails": [], "mobiles": ["13800138000"],
            "phones": [], "names": [], "legal": [], "contacts": [],
            "snippet": "我的私人手机 13800138000 周末爬山"}
HI_TEXT = "我的私人手机 13800138000 周末爬山"
out_hi = scraper._apply_compliance(make_hi(), HI_TEXT, "search", include_personal=False)
check("高风险默认丢弃", out_hi is None)
out_hi2 = scraper._apply_compliance(make_hi(), HI_TEXT, "search", include_personal=True)
check("高风险开启后保留", out_hi2 is not None and "13800138000" in out_hi2["mobiles"])

print("== 拒收名单 DNC ==")
compliance.dnc_add("13912345678", "本人要求删除", "test")
check("加入后可查", compliance.dnc_blocked("13912345678"))
check("未加入不拦", not compliance.dnc_blocked("13900000000"))
compliance.dnc_remove("13912345678")
check("移除后不拦", not compliance.dnc_blocked("13912345678"))

print("== 跨源合并 merge_by_mobile ==")
r1 = {"source": "https://a.com", "sources": ["https://a.com"], "emails": ["a@x.com"], "mobiles": ["13800138000"],
      "mobile_carriers": {"13800138000": "中国移动"}, "phones": [], "names": ["张三"], "legal": [],
      "contacts": [{"name": "张三", "mobile": "13800138000", "carrier": "中国移动"}], "snippet": "s1",
      "confidence": "high", "context": "personal_in_business", "risk": "medium", "business_context": True,
      "legal_basis": "x", "source_type": "search", "collected_at": "2026-08-18T00:00:00+00:00"}
r2 = {"source": "https://b.com", "sources": ["https://b.com"], "emails": ["b@x.com"], "mobiles": ["13800138000"],
      "mobile_carriers": {"13800138000": "中国移动"}, "phones": ["010-1234"], "names": ["李四"], "legal": [],
      "contacts": [{"name": "李四", "mobile": "13800138000", "carrier": "中国移动"}], "snippet": "s2",
      "confidence": "low", "context": "business_line", "risk": "low", "business_context": True,
      "legal_basis": "y", "source_type": "social:wechat", "collected_at": "2026-08-18T01:00:00+00:00"}
merged = scraper.merge_by_mobile([r1, r2])
check("合并为 1 条", len(merged) == 1)
check("来源数=2", merged[0]["count"] == 2)
check("邮箱聚合", set(merged[0]["emails"]) == {"a@x.com", "b@x.com"})
check("联系人聚合", len(merged[0]["contacts"]) == 2)
check("取最高风险", merged[0]["risk"] == "medium")

print(f"\n结果：{PASS} 通过，{FAIL} 失败")
sys.exit(1 if FAIL else 0)
