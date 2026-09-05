"""合成数据离线质量评估；结果不代表真实店铺生产表现。"""

import csv
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.main import demo_listing, inventory_status, product_score, ticket_rules, validate_listing  # noqa: E402


def ratio(passed: int, total: int) -> dict[str, int | float]:
    return {"通过数": passed, "总数": total, "通过率（百分比）": round(passed / total * 100, 1)}


def main() -> None:
    with (ROOT / "samples" / "products.csv").open(encoding="utf-8-sig", newline="") as handle:
        products = list(csv.DictReader(handle))

    listing_passed = 0
    for product in products:
        try:
            validate_listing(demo_listing(product))
            listing_passed += 1
        except ValueError:
            pass

    checks = [
        inventory_status(10, 1, 10)["level"] == "critical",
        inventory_status(17, 1, 10)["level"] == "warning",
        inventory_status(18, 1, 10)["level"] == "healthy",
        ticket_rules(1, True)["priority"] == "P0",
        ticket_rules(2, False)["priority"] == "P1",
        ticket_rules(5, False)["priority"] == "P2",
    ]
    workflow_checks = []
    for product in products:
        score, breakdown = product_score(product)
        workflow_checks.append(0 <= score <= 100 and round(sum(breakdown.values()), 2) == score)

    report = {
        "证据范围": "合成数据离线演示",
        "结构化商品文案校验": ratio(listing_passed, len(products)),
        "确定性规则边界校验": ratio(sum(checks), len(checks)),
        "样例商品分析": ratio(sum(workflow_checks), len(workflow_checks)),
        "是否属于生产环境结果": False,
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
