"""价格与品类的配对回归测试。

存在的原因：第一版按「距离最近」配对，在真实报价单上四个配对全错 ——
「Claude 5x 278rmb Claude 20x 475rmb」里的 278 被配给了 Claude 20x，
因为它离得更近。整条价格基线会系统性错位，而且错得很隐蔽（数字都合理）。
"""
import os

os.environ.setdefault("TELEGRAM_API_ID", "12345")
os.environ.setdefault("TELEGRAM_API_HASH", "dummy_hash")

from market_watch import extract_priced_items, USD_CNY


def pairs(text):
    return [(it["raw"], it["category"]) for it in extract_priced_items(text)]


def test_real_price_list_pairs_left_not_nearest():
    """真实报价单：每个价格都属于它**前面**那个品类。"""
    got = dict(pairs("Claude 5x 278rmb Claude 20x 475rmb GPT Pro 5x 130rmb GPT Pro 20x 180rmb"))
    assert got["278rmb"] == "Claude 5x"
    assert got["475rmb"] == "Claude 20x"
    assert got["130rmb"] == "GPT Pro 5x"
    assert got["180rmb"] == "GPT Pro 20x"


def test_price_before_category_still_pairs():
    """少数写法是「价格 品类」，前面没有品类时才回退到后面那个。"""
    assert dict(pairs("低至 9元 GPT Plus 现货"))["9元"] == "GPT Plus"


def test_usd_converted_to_cny():
    items = extract_priced_items("GPT Pro 20x 25刀")
    assert items and items[0]["cny"] == round(25 * USD_CNY, 2)


def test_far_number_is_not_paired():
    """配对窗口挡掉噪音：4-29 语料里有「价格比 GPT-5.4」这类误配。"""
    text = "GPT Plus 现货充足质保三十天售后无忧欢迎联系客服详询另有其他商品 128元"
    assert extract_priced_items(text) == []


def test_no_category_no_pair():
    assert extract_priced_items("一共 200元 包邮") == []


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"  ✓ {name}")
    print("全部通过")
