import csv
import json
from contextlib import closing
from pathlib import Path

from app.main import connect, demo_listing, demo_ticket_reply, ticket_rules


ROOT = Path(__file__).resolve().parents[1]


def read_rows(name: str) -> list[dict[str, str]]:
    with (ROOT / "samples" / name).open(encoding="utf-8", newline="") as file:
        return list(csv.DictReader(file))


def main() -> None:
    products = read_rows("products.csv")
    tickets = read_rows("tickets.csv")
    with closing(connect()) as conn:
        for row in products:
            conn.execute(
                "UPDATE products SET source_title=?, category=? WHERE sku=?",
                (row["source_title"], row["category"], row["sku"]),
            )

        for row in tickets:
            rules = ticket_rules(int(row["rating"]), row["refund_requested"].lower() == "true")
            conn.execute(
                "UPDATE tickets SET rating=?, refund_requested=?, title=?, message=?, topic=?, priority=?, sla=? WHERE sku=?",
                (int(row["rating"]), int(row["refund_requested"].lower() == "true"), row["title"], row["message"], rules["topic"], rules["priority"], rules["sla"], row["sku"]),
            )

        draft_rows = conn.execute(
            "SELECT l.id,p.* FROM listings l JOIN products p ON p.id=l.product_id WHERE l.provider='demo' AND l.status='draft'"
        ).fetchall()
        for draft in draft_rows:
            listing = demo_listing(dict(draft))
            conn.execute(
                "UPDATE listings SET title=?,bullet_points=?,description=?,search_terms=?,compliance_warnings=? WHERE id=?",
                (listing["title"], json.dumps(listing["bullet_points"], ensure_ascii=False), listing["description"], json.dumps(listing["search_terms"], ensure_ascii=False), json.dumps(listing["compliance_warnings"], ensure_ascii=False), draft["id"]),
            )

        reply, action = demo_ticket_reply({})
        conn.execute("UPDATE tickets SET reply_draft=?,action_plan=? WHERE reply_draft IS NOT NULL", (reply, action))
        conn.commit()
        assert conn.execute("SELECT COUNT(*) FROM products WHERE source_title LIKE ?", ("%炉边优选%",)).fetchone()[0] == 2
        assert conn.execute("SELECT COUNT(*) FROM tickets WHERE title=?", ("使用一周后无法充电",)).fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM listings WHERE status='draft' AND title LIKE ?", ("%适合日常使用%",)).fetchone()[0] == len(draft_rows)
    print(f"localized_products={len(products)} localized_tickets={len(tickets)} localized_draft_listings={len(draft_rows)}")


if __name__ == "__main__":
    main()
