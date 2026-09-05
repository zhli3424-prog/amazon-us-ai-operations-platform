import importlib
import io
import json
import csv
import base64
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


PRODUCT_CSV = b"""sku,asin,source_title,category,price,cost,monthly_sales,rating,review_count,competition_score,stock_units,daily_sales,lead_time_days
SKU-1,B0TEST000001,Demo Kitchen Scale,kitchen scale,20,8,800,4.5,120,40,20,4,10
SKU-2,B0TEST000002,Demo Vacuum,vacuum,40,18,500,4.0,50,60,100,5,14
"""


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("MEDIA_ROOT", str(tmp_path / "media"))
    monkeypatch.setenv("PRIVATE_ROOT", str(tmp_path / "private"))
    monkeypatch.setenv("APP_ENV", "testing")
    monkeypatch.setenv("SESSION_SECRET", "test-session-secret-with-at-least-32-characters")
    monkeypatch.setenv("ADMIN_PASSWORD", "TestAdmin123!ChangeMe")
    monkeypatch.delenv("AI_API_KEY", raising=False)
    import app.main as main
    importlib.reload(main)
    with TestClient(main.app) as test_client:
        login = test_client.post("/login", data={"username": "admin", "password": "TestAdmin123!ChangeMe"})
        assert login.status_code == 200
        session = main.read_session(test_client.cookies.get("ops_session"), main.SESSION_SECRET)
        test_client.headers["X-CSRF-Token"] = session["csrf"]
        yield test_client, main


def upload(client, path, content, filename="data.csv"):
    return client.post(path, files={"file": (filename, io.BytesIO(content), "text/csv")})


def verify_evidence(api, main, product_id, fields=("source_title",)):
    with main.connect() as conn:
        rows = conn.execute("SELECT id,version,field_name FROM product_evidence WHERE product_id=? AND status='pending'", (product_id,)).fetchall()
    found = set()
    for row in rows:
        if row["field_name"] in fields and row["field_name"] not in found:
            assert api.post(f"/api/evidence/{row['id']}/verify", data={"version": row["version"]}).status_code == 200
            found.add(row["field_name"])
    assert found == set(fields)


def test_complete_listing_workflow(client):
    api, main = client
    result = upload(api, "/api/import/products", PRODUCT_CSV)
    assert result.status_code == 200
    assert result.json() == {"imported": 2, "skipped": 0, "errors": []}
    verify_evidence(api, main, 1)

    score = api.post("/api/products/1/analyze")
    assert score.status_code == 200
    assert score.json()["score"] == 72.0
    assert set(score.json()["breakdown"]) == {"demand", "margin", "rating", "competition"}

    generated = api.post("/api/products/1/listing/generate")
    assert generated.status_code == 200
    listing = generated.json()
    assert listing["provider"] == "demo"
    assert len(listing["bullet_points"]) == 5
    assert len(listing["bullet_points_zh"]) == 5
    assert not any("\u4e00" <= char <= "\u9fff" for char in listing["title"])
    assert any("\u4e00" <= char <= "\u9fff" for char in listing["title_zh"])
    assert "差异化双语草稿" in listing["review_notes_cn"]

    blocked = api.post(f"/api/listings/{listing['listing_id']}/publish", data={"version": 1})
    assert blocked.status_code == 409

    approved = api.post(
        f"/api/listings/{listing['listing_id']}/approve",
        data={
            "title": "Edited US Title",
            "bullet_points": "One\nTwo\nThree\nFour\nFive",
            "description": "Human reviewed description.",
            "search_terms": "scale, kitchen",
            "title_zh": "人工审核厨房秤",
            "bullet_points_zh": "操作直观\n使用方便\n便于收纳\n信息透明\n购买前核对规格",
            "description_zh": "这是人工审核后的中文商品描述。",
            "search_terms_zh": "厨房秤，电子秤",
            "version": 1,
        },
    )
    assert approved.json()["status"] == "approved"
    published = api.post(f"/api/listings/{listing['listing_id']}/publish", data={"version": 2})
    assert published.json()["status"] == "mock_published"

    with main.connect() as conn:
        snapshot = json.loads(conn.execute("SELECT snapshot FROM publish_records").fetchone()[0])
        assert snapshot["title"] == "Edited US Title"
        assert snapshot["title_zh"] == "人工审核厨房秤"
        assert snapshot["amazon_us_payload"]["title"] == "Edited US Title"
        assert snapshot["internal_zh_copy"]["title_zh"] == "人工审核厨房秤"


def test_csv_partial_failure_and_duplicate(client):
    api, _ = client
    bad = PRODUCT_CSV + b"SKU-3,B0TEST000003,Bad Product,kitchen,-1,2,20,8,0,20,1,1,2\n"
    first = upload(api, "/api/import/products", bad).json()
    assert first["imported"] == 2
    assert first["errors"][0]["line"] == 4
    second = upload(api, "/api/import/products", PRODUCT_CSV).json()
    assert second["skipped"] == 2
    assert upload(api, "/api/import/products", PRODUCT_CSV, "data.txt").status_code == 400


def test_inventory_boundaries_and_ticket_priority(client):
    api, main = client
    upload(api, "/api/import/products", PRODUCT_CSV)
    assert main.inventory_status(10, 1, 10)["level"] == "critical"
    assert main.inventory_status(17, 1, 10)["level"] == "warning"
    assert main.inventory_status(18, 1, 10)["level"] == "healthy"
    assert main.inventory_status(10, 0, 10) == {"days": None, "level": "unknown", "label": "缺少销量数据", "reorder_point": None, "reorder_qty": 0}
    assert main.ticket_rules(1, True)["priority"] == "P0"

    tickets = b"sku,rating,refund_requested,title,message\nSKU-1,1,true,Broken,Stopped working\nSKU-2,5,false,Good,Works well\n"
    result = upload(api, "/api/tickets/import", tickets).json()
    assert result["imported"] == 2
    drafted = api.post("/api/tickets/1/draft-reply")
    assert drafted.status_code == 200
    assert drafted.json()["status"] == "draft"
    assert drafted.json()["reply_draft"].startswith("Hello")
    payload = drafted.json()
    approved = api.post(f"/api/tickets/1/messages/{payload['message_id']}/approve", data={"message_version": payload["message_version"]})
    assert approved.status_code == 200 and approved.json()["status"] == "approved"
    failed = api.post(f"/api/tickets/1/messages/{payload['message_id']}/send-demo", data={"message_version": approved.json()["version"], "simulate_failure": "true"})
    assert failed.status_code == 200 and failed.json()["status"] == "failed" and failed.json()["real_external_send"] is False
    retried = api.post(f"/api/tickets/1/messages/{payload['message_id']}/send-demo", data={"message_version": failed.json()["version"]})
    assert retried.status_code == 200 and retried.json()["status"] == "sent_demo" and retried.json()["real_external_send"] is False


def test_ai_validation_and_safe_failure(client, monkeypatch):
    api, main = client
    with pytest.raises(ValueError, match="有效 JSON"):
        main.extract_json("not-json")
    invalid = main.demo_listing({"source_title": "X", "category": "tool"})
    invalid["bullet_points"] = ["only one"]
    with pytest.raises(ValueError, match="5 条"):
        main.validate_listing(invalid)

    upload(api, "/api/import/products", PRODUCT_CSV)
    monkeypatch.setenv("AI_API_KEY", "super-secret-test-key")
    monkeypatch.setattr(main.httpx, "post", lambda *a, **k: (_ for _ in ()).throw(main.httpx.TimeoutException("timeout")))
    result = api.post("/api/products/1/listing/generate")
    assert result.status_code == 502
    assert "super-secret-test-key" not in result.text


def test_pages_and_health(client):
    api, _ = client
    assert api.get("/").status_code == 200
    for path in ("dashboard", "orders", "products", "profit", "listings", "inventory", "procurement", "tickets", "feedback", "audit", "settings"):
        response = api.get(f"/{path}")
        assert response.status_code == 200
        assert "跨境智营台" in response.text
    health = api.get("/health")
    assert health.status_code == 200
    assert health.json()["database"] == "ok"
    assert health.json()["ai_provider"] == "demo"


def test_realistic_sample_dataset_imports_and_links(client):
    api, main = client
    samples = Path(__file__).resolve().parent.parent / "samples"
    products = upload(api, "/api/import/products", (samples / "products.csv").read_bytes()).json()
    orders = api.post("/api/channels/amazon-us/sync/orders").json()
    inventory = api.post("/api/channels/amazon-us/sync/inventory").json()
    tickets = api.post("/api/channels/amazon-us/sync/tickets").json()
    feedback = api.post("/api/channels/amazon-us/sync/feedback").json()

    assert products == {"imported": 20, "skipped": 0, "errors": []}
    assert orders["inserted"] == 8 and orders["errors"] == 0
    assert inventory["inserted"] == 20 and inventory["errors"] == 0
    assert tickets["inserted"] == 8 and tickets["errors"] == 0
    assert feedback["inserted"] == 10 and feedback["errors"] == 0
    assert inventory["connection_mode"] == "demo_sp_api"
    assert api.post("/api/channels/amazon-us/sync/inventory").json()["skipped"] == 20
    with main.connect() as conn:
        product_rows = [dict(row) for row in conn.execute("SELECT * FROM products")]
        ticket_skus = {row[0] for row in conn.execute("SELECT DISTINCT sku FROM tickets WHERE is_archived=0")}
        assert conn.execute("SELECT COUNT(*) FROM inventory_snapshots").fetchone()[0] == 20
        assert conn.execute("SELECT COUNT(*) FROM feedback_insights").fetchone()[0] == 10
        assert conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 8
        assert conn.execute("SELECT COUNT(*) FROM order_items").fetchone()[0] == 8
        assert conn.execute("SELECT COUNT(*) FROM returns").fetchone()[0] == 2
        assert conn.execute("SELECT COUNT(*) FROM tickets WHERE is_archived=0 AND order_ref_id IS NOT NULL").fetchone()[0] == 8
        assert {row[0] for row in conn.execute("SELECT DISTINCT event_type FROM tickets WHERE is_archived=0")} <= {"order_exception", "buyer_cancel", "return_refund", "buyer_message"}
        assert all("***" in row[0] for row in conn.execute("SELECT order_id_masked FROM tickets WHERE is_archived=0"))
    product_skus = {row["sku"] for row in product_rows}
    assert ticket_skus <= product_skus
    assert len({row["asin"] for row in product_rows}) == 20
    assert all(len(row["asin"]) == 10 for row in product_rows)
    image_dir = samples.parent / "app" / "static" / "products"
    assert {path.stem for path in image_dir.glob("*.png")} == product_skus
    product_pages = api.get("/products").text + api.get("/products?page_no=2").text
    assert all(f"/static/products/{sku}.png" in product_pages for sku in product_skus)
    inventory_page = api.get("/inventory").text
    order_page = api.get("/orders").text
    ticket_page = api.get("/tickets").text
    feedback_page = api.get("/feedback").text
    profit_page = api.get("/profit").text
    assert 'type="file"' not in inventory_page
    assert "/api/tickets/import" not in ticket_page
    assert "上传私有附件" in ticket_page
    assert "同步亚马逊库存" in inventory_page
    assert "同步亚马逊订单" in order_page and "RET-9306-01" in order_page
    assert "同步客户事件" in ticket_page
    assert all(f"/static/products/{sku}.png" in inventory_page for sku in product_skus)
    assert all(f"/static/products/{sku}.png" in profit_page for sku in product_skus)
    assert all(f"/static/products/{sku}.png" in ticket_page for sku in ticket_skus)
    feedback_skus = {row["sku"] for row in main.load_demo_channel("feedback")}
    assert all(f"/static/products/{sku}.png" in feedback_page for sku in feedback_skus)
    order_skus = {item["sku"] for row in main.load_demo_channel("orders") for item in row["items"]}
    assert all(f"/static/products/{sku}.png" in order_page for sku in order_skus)
    api.post("/api/products/1/listing/generate")
    listing_page = api.get("/listings").text
    assert "/static/products/KT-SCALE-5K-US.png" in listing_page
    assert 'name="title_zh"' in listing_page and "运营审核译稿" in listing_page
    risk_levels = {main.inventory_status(row["stock_units"], row["daily_sales"], row["lead_time_days"])["level"] for row in product_rows}
    assert risk_levels == {"critical", "warning", "healthy"}


def test_order_sync_is_idempotent_and_links_support_tickets(client):
    api, main = client
    upload(api, "/api/import/products", (Path(__file__).resolve().parent.parent / "samples" / "products.csv").read_bytes())
    first = api.post("/api/channels/amazon-us/sync/orders").json()
    second = api.post("/api/channels/amazon-us/sync/orders").json()
    assert first["inserted"] == 8 and second["skipped"] == 8
    api.post("/api/channels/amazon-us/sync/tickets")
    with main.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 8
        assert conn.execute("SELECT COUNT(*) FROM order_items").fetchone()[0] == 8
        linked = conn.execute("SELECT COUNT(*) FROM tickets WHERE order_ref_id IS NOT NULL").fetchone()[0]
        refund = conn.execute("SELECT refund_amount FROM tickets WHERE external_event_id='EVT-RETURN-20260901-002'").fetchone()[0]
    assert linked == 8 and refund == 27.99
    ticket_page = api.get("/tickets").text
    assert "已关联订单" in ticket_page and "1Z***7742" in ticket_page


def test_ticket_history_escalation_reopen_and_service_action_gate(client):
    api, main = client
    samples = Path(__file__).resolve().parent.parent / "samples"
    upload(api, "/api/import/products", (samples / "products.csv").read_bytes())
    api.post("/api/channels/amazon-us/sync/orders")
    api.post("/api/channels/amazon-us/sync/tickets")
    with main.connect() as conn:
        ticket_id = conn.execute("SELECT id FROM tickets WHERE external_event_id='EVT-RETURN-20260901-002'").fetchone()[0]
    note = api.post(f"/api/tickets/{ticket_id}/notes", data={"version": 1, "body": "已核对退货批次，等待仓库检测"})
    assert note.status_code == 200 and note.json()["version"] == 2
    escalated = api.post(f"/api/tickets/{ticket_id}/escalate", data={"version": 2, "escalation_level": "operations_manager", "reason": "退款金额需运营经理复核"})
    assert escalated.status_code == 200
    requested = api.post(f"/api/tickets/{ticket_id}/service-actions", data={"action_type": "refund", "amount": 27.99, "reason": "退回件确认故障"})
    assert requested.status_code == 200
    action_id = requested.json()["action_id"]
    assert api.post(f"/api/service-actions/{action_id}/complete").status_code == 409
    assert api.post(f"/api/service-actions/{action_id}/approve").json()["status"] == "approved"
    completed = api.post(f"/api/service-actions/{action_id}/complete").json()
    assert completed == {"action_id": action_id, "status": "completed", "external_execution": False}
    assert api.post(f"/api/tickets/{ticket_id}/update", data={"assigned_to": "售后主管·林悦", "status": "processing", "resolution": "", "version": 3}).status_code == 200
    assert api.post(f"/api/tickets/{ticket_id}/update", data={"assigned_to": "售后主管·林悦", "status": "resolved", "resolution": "退款审批与仓库检测均已登记", "version": 4}).status_code == 200
    reopened = api.post(f"/api/tickets/{ticket_id}/reopen", data={"version": 5, "reason": "买家补充了新的问题说明"})
    assert reopened.status_code == 200 and reopened.json()["status"] == "processing"
    with main.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM ticket_events WHERE ticket_id=?", (ticket_id,)).fetchone()[0] >= 8
        assert conn.execute("SELECT reopen_count FROM tickets WHERE id=?", (ticket_id,)).fetchone()[0] == 1
    page = api.get("/tickets").text
    assert "售后执行与完整记录" in page and "退款金额需运营经理复核" in page


def test_replenishment_purchase_order_approval_and_receipt(client):
    api, main = client
    samples = Path(__file__).resolve().parent.parent / "samples"
    upload(api, "/api/import/products", (samples / "products.csv").read_bytes())
    api.post("/api/channels/amazon-us/sync/inventory")
    with main.connect() as conn:
        product_id = conn.execute("SELECT id FROM products WHERE sku='HM-VAC-02-US'").fetchone()[0]
        stock_before = conn.execute("SELECT stock_units FROM products WHERE id=?", (product_id,)).fetchone()[0]
    created = api.post(f"/api/products/{product_id}/replenishment-plans", data={"safety_days": 14, "quantity": 180, "reason": "覆盖采购交期和安全库存"})
    assert created.status_code == 200 and created.json()["status"] == "draft"
    plan_id = created.json()["plan_id"]
    assert api.post(f"/api/products/{product_id}/replenishment-plans", data={"quantity": 180, "reason": "重复计划"}).status_code == 409
    approved = api.post(f"/api/replenishment-plans/{plan_id}/approve", data={"version": 1, "approved_qty": 160})
    assert approved.status_code == 200
    converted = api.post(f"/api/replenishment-plans/{plan_id}/convert").json()
    po_id = converted["purchase_order_id"]
    assert converted["status"] == "draft"
    assert api.post(f"/api/purchase-orders/{po_id}/approve", data={"version": 1}).json()["status"] == "approved"
    assert api.post(f"/api/purchase-orders/{po_id}/send-demo", data={"version": 2}).json()["external_execution"] is False
    with main.connect() as conn:
        item_id = conn.execute("SELECT id FROM purchase_order_items WHERE purchase_order_id=?", (po_id,)).fetchone()[0]
    partial = api.post(f"/api/purchase-order-items/{item_id}/receive", data={"quantity": 55, "rejected_quantity": 5, "note": "首批抽检发现 5 件外观不良"}).json()
    assert partial["status"] == "partially_received" and partial["inventory_effect"] == "warehouse_on_hand" and partial["rejected_qty"] == 5
    received = api.post(f"/api/purchase-order-items/{item_id}/receive", data={"quantity": 100, "note": "尾批到货验收合格"}).json()
    assert received["status"] == "received"
    assert api.post(f"/api/purchase-order-items/{item_id}/receive", data={"quantity": 1, "note": "超额"}).status_code == 409
    with main.connect() as conn:
        assert conn.execute("SELECT stock_units FROM products WHERE id=?", (product_id,)).fetchone()[0] == stock_before
        assert conn.execute("SELECT SUM(quantity) FROM inventory_movements WHERE reference_id=? AND movement_type='purchase_receipt'", (item_id,)).fetchone()[0] == 155
        warehouse = conn.execute("SELECT on_hand,qc_hold FROM warehouse_inventory WHERE sku='HM-VAC-02-US'").fetchone()
        assert tuple(warehouse) == (155, 5)
    page = api.get("/procurement").text
    assert "采购单与质检收货" in page and converted["po_number"] in page


def test_job_dedup_progress_stale_recovery_retry_and_worker_health(client, monkeypatch):
    api, main = client
    monkeypatch.setattr(main, "SYNC_INLINE", False)
    first = main.enqueue_sync_job("inventory")
    duplicate = main.enqueue_sync_job("inventory")
    assert duplicate == first
    job = api.get(f"/api/jobs/{first}").json()
    assert job["status"] == "queued" and job["progress"] == 0
    stale = "2020-01-01T00:00:00+00:00"
    with main.connect() as conn:
        conn.execute("UPDATE jobs SET status='running',attempts=1,locked_at=?,heartbeat_at=?,progress=25 WHERE id=?", (stale, stale, first))
        conn.commit()
    assert main.recover_stale_jobs() == 1
    assert api.get(f"/api/jobs/{first}").json()["status"] == "queued"
    with main.connect() as conn:
        conn.execute("UPDATE jobs SET status='failed',attempts=max_attempts,completed_at=?,current_step='已进入失败队列' WHERE id=?", (main.now(), first))
        conn.commit()
    retried = api.post(f"/api/jobs/{first}/retry").json()
    assert retried["job_status"] == "queued"
    monkeypatch.setattr(main, "SYNC_INLINE", True)
    main.update_worker_heartbeat("idle")
    assert main.worker_health()["healthy"] is True

    upload(api, "/api/import/products", PRODUCT_CSV)
    completed = api.post("/api/channels/amazon-us/sync/inventory")
    assert completed.status_code == 200
    completed_job = api.get(f"/api/jobs/{completed.json()['job_id']}").json()
    assert completed_job["status"] == "succeeded" and completed_job["progress"] == 100
    assert completed_job["result"]["sync_type"] == "inventory"


def test_finance_records_cost_config_inventory_history_and_market_score(client):
    api, main = client
    samples = Path(__file__).resolve().parent.parent / "samples"
    upload(api, "/api/import/products", (samples / "products.csv").read_bytes())
    api.post("/api/channels/amazon-us/sync/orders")
    finance = api.post("/api/channels/amazon-us/sync/finance").json()
    assert finance["inserted"] > 30 and finance["errors"] == 0
    assert api.post("/api/channels/amazon-us/sync/finance").json()["skipped"] == finance["inserted"]
    context = main.page_context("profit")
    settled = [p for p in context["products"] if p["economics"]["data_source"] == "结算流水"]
    assert len(settled) == 8 and context["business_totals"]["sku_coverage"] == 8
    assert context["business_totals"]["data_source"] == "结算流水"
    assert sum(main.allocate_cents(1001, [1000, 2000, 3000])) == 1001
    with main.connect() as conn:
        transaction = conn.execute("SELECT amount,amount_cents,currency FROM financial_transactions LIMIT 1").fetchone()
        assert transaction["amount_cents"] == round(transaction["amount"] * 100) and transaction["currency"] == "USD"
        conn.execute("UPDATE financial_transactions SET posted_at='2020-01-01T00:00:00+00:00'")
        conn.commit()
    empty_period = main.page_context("profit", {"date_from": "2026-09-01", "date_to": "2026-09-05"})
    assert empty_period["business_totals"]["data_source"] == "费率配置估算"

    first_inventory = api.post("/api/channels/amazon-us/sync/inventory").json()
    second_inventory = api.post("/api/channels/amazon-us/sync/inventory").json()
    assert first_inventory["inserted"] == 20 and second_inventory["skipped"] == 20
    with main.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM inventory_history").fetchone()[0] == 40

    market = api.post("/api/market-research/sync-demo").json()
    assert market["inserted"] == 6
    with main.connect() as conn:
        product_id = conn.execute("SELECT id FROM products WHERE sku='HM-VAC-02-US'").fetchone()[0]
    scored = api.post(f"/api/products/{product_id}/analyze").json()
    assert scored["basis"] == "外部市场研究快照"
    assert set(scored["breakdown"]) == {"market_demand", "margin", "competitor_density", "review_barrier", "market_trend"}

    updated = api.post(f"/api/products/{product_id}/cost-profile", data={"referral_rate": 0.12, "fba_fee_per_unit": 4.1, "inbound_cost_per_unit": 0.9, "ad_rate": 0.08, "other_cost_per_unit": 0.2, "effective_from": "2026-09-01"})
    assert updated.status_code == 200
    profit_page = api.get("/profit").text
    assert "同步结算流水" in profit_page and "成本口径与数据来源" in profit_page
    assert "库存历史快照" in api.get("/inventory").text
    assert "外部市场研究快照" in main.page_context("products")["products"][0].get("score_basis", "") or scored["basis"] == "外部市场研究快照"


def test_listing_evidence_compliance_trademark_and_ai_usage(client):
    api, main = client
    upload(api, "/api/import/products", PRODUCT_CSV)
    generated = api.post("/api/products/1/listing/generate").json()
    assert generated["compliance_status"] == "blocked"
    blocked = api.post(f"/api/listings/{generated['listing_id']}/approve", data={"version": 1})
    assert blocked.status_code == 422 and "缺少已核验证据字段" in blocked.text
    verify_evidence(api, main, 1)
    approved = api.post(f"/api/listings/{generated['listing_id']}/approve", data={"version": 1})
    assert approved.status_code == 200
    with main.connect() as conn:
        run = dict(conn.execute("SELECT * FROM ai_runs WHERE run_type='listing'").fetchone())
    assert run["prompt_version"] == main.LISTING_PROMPT_VERSION
    assert run["input_tokens"] > 0 and run["output_tokens"] > 0 and run["estimated_cost_usd"] == 0

    second = api.post("/api/products/2/listing/generate").json()
    verify_evidence(api, main, 2)
    forbidden = api.post(f"/api/listings/{second['listing_id']}/approve", data={
        "version": 1, "title": "Kindle Compatible Demo Vacuum", "bullet_points": "One\nTwo\nThree\nFour\nFive",
        "description": "A careful generic description.", "search_terms": "vacuum, home",
        "title_zh": "演示吸尘器", "bullet_points_zh": "第一点\n第二点\n第三点\n第四点\n第五点",
        "description_zh": "中文审核描述。", "search_terms_zh": "吸尘器，家居",
    })
    assert forbidden.status_code == 422 and "kindle" in forbidden.text.lower()
    listing_page = api.get("/listings").text
    assert "AI 调用与合规追踪" in listing_page and main.LISTING_PROMPT_VERSION in listing_page


def test_product_master_image_history_pagination_and_listing_bulk_review(client):
    api, main = client
    rich_csv = PRODUCT_CSV.decode().splitlines()
    rich_csv[0] += ",brand,material,dimensions_cm,weight_kg,supplier_name,moq,purchase_lead_days,origin_country,upc_ean,compliance_tags,evidence_notes,status"
    rich_csv[1] += ",NorthPeak,Stainless steel,20 x 15 x 3,0.55,Shenzhen Demo Supply,100,21,CN,012345678905,FCC;RoHS,Supplier spec dated 2026-08-01,active"
    rich_csv[2] += ",HomeFlow,ABS,35 x 12 x 10,1.2,Ningbo Demo Supply,50,30,CN,012345678912,UL,Lab report pending,active"
    assert upload(api, "/api/import/products", "\n".join(rich_csv).encode()).json()["imported"] == 2
    updated = api.post("/api/products/1/update", data={
        "version": 1, "source_title": "Precision Kitchen Scale", "category": "kitchen scale", "brand": "NorthPeak",
        "price": 22.99, "cost": 8.5, "material": "304 stainless steel", "dimensions_cm": "20 x 15 x 3",
        "weight_kg": 0.56, "supplier_name": "Shenzhen Demo Supply", "supplier_sku": "SZ-SCALE-01",
        "moq": 120, "purchase_lead_days": 24, "origin_country": "CN", "upc_ean": "012345678905",
        "compliance_tags": "FCC，RoHS", "evidence_notes": "Supplier spec verified 2026-08-10", "status": "active",
        "reason": "供应商规格复核",
    })
    assert updated.status_code == 200 and updated.json()["version"] == 2
    conflict = api.post("/api/products/1/update", data={
        "version": 1, "source_title": "Stale", "category": "kitchen", "price": 1, "cost": 1, "moq": 1,
        "purchase_lead_days": 1, "status": "active", "reason": "旧页面提交",
    })
    assert conflict.status_code == 409
    image = api.post("/api/products/1/image", data={"version": 2}, files={"file": ("scale.png", b"\x89PNG\r\n\x1a\n" + b"demo", "image/png")})
    assert image.status_code == 200 and image.json()["image_url"].startswith("/media/products/")
    history = api.get("/api/products/1/history").json()["changes"]
    assert [item["version"] for item in history] == [3, 2, 1]
    assert "Precision Kitchen Scale" in api.get("/products?q=Precision").text
    verify_evidence(api, main, 1)
    verify_evidence(api, main, 2)

    first = api.post("/api/products/1/listing/generate").json()
    second = api.post("/api/products/2/listing/generate").json()
    items = f"{first['listing_id']}:1,{second['listing_id']}:1"
    bulk = api.post("/api/listings/bulk-approve", data={"items": items})
    assert bulk.status_code == 200 and len(bulk.json()["approved"]) == 2
    rejected = api.post(f"/api/listings/{first['listing_id']}/reject", data={"version": 2, "reason": "规格证据不足"})
    assert rejected.status_code == 200
    revisions = api.get(f"/api/listings/{first['listing_id']}/revisions").json()["revisions"]
    assert {item["action"] for item in revisions} >= {"generated", "bulk_approved", "rejected"}
    assert "规格证据不足" in api.get("/listings?listing_status=rejected").text


def test_channel_status_hidden_csv_and_safe_row_failures(client, monkeypatch):
    api, main = client
    upload(api, "/api/import/products", PRODUCT_CSV)
    status = api.get("/api/channels/amazon-us/status").json()
    assert status["mode"] == "demo_sp_api"
    assert "模拟 SP-API" in status["connection_label"]
    paths = api.get("/openapi.json").json()["paths"]
    assert "/api/import/inventory" not in paths
    assert "/api/tickets/import" not in paths
    assert upload(api, "/api/import/inventory", b"sku,stock_units,daily_sales,lead_time_days\nSKU-1,7,1,5\n").status_code == 200

    bad_rows = [
        {"external_event_id": "bad-negative", "sku": "SKU-1", "fulfillable": -1, "reserved": 0, "inbound": 0, "unfulfillable": 0, "daily_sales": 1, "lead_time_days": 5},
        {"external_event_id": "bad-sku", "sku": "MISSING", "fulfillable": 1, "reserved": 0, "inbound": 0, "unfulfillable": 0, "daily_sales": 1, "lead_time_days": 5},
    ]
    result = main.sync_inventory_rows(bad_rows)
    assert result["errors"] == 2
    with main.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM inventory_snapshots").fetchone()[0] == 0


def test_fulfillable_inventory_drives_days(client):
    api, main = client
    upload(api, "/api/import/products", PRODUCT_CSV)
    rows = [{"external_event_id": "one", "sku": "SKU-1", "fulfillable": 4, "reserved": 100, "inbound": 200, "unfulfillable": 20, "daily_sales": 2, "lead_time_days": 5}]
    assert main.sync_inventory_rows(rows)["inserted"] == 1
    context = main.page_context("inventory")
    product = next(item for item in context["products"] if item["sku"] == "SKU-1")
    assert product["daily_sales"] == 4 and product["inventory"]["days"] == 1.0
    assert product["inventory"]["level"] == "critical"
    assert product["inventory"]["reorder_qty"] == 0


def test_profit_metrics_and_ticket_resolution_gate(client):
    api, main = client
    upload(api, "/api/import/products", PRODUCT_CSV)
    economics = main.product_economics(dict(main.connect().execute("SELECT * FROM products WHERE sku='SKU-1'").fetchone()))
    assert economics["revenue"] == 16000.0
    assert economics["net_profit"] > 0
    assert economics["margin"] < 100
    dashboard = api.get("/dashboard").text
    assert "贡献利润" in dashboard and "今日经营待办" in dashboard

    main.sync_ticket_rows([{
        "external_event_id": "ticket-profit-test", "sku": "SKU-1", "event_type": "return_refund",
        "source": "FBA Customer Returns Report", "order_id_masked": "114-***-0001", "customer_alias": "客户 T***1",
        "event_at": "2026-09-03T01:00:00Z", "rating": 1, "refund_requested": True,
        "title": "Return", "message": "Item not working",
    }])
    with main.connect() as conn:
        ticket_id = conn.execute("SELECT id FROM tickets WHERE external_event_id='ticket-profit-test'").fetchone()[0]
    missing = api.post(f"/api/tickets/{ticket_id}/update", data={"assigned_to": "客服·周宁", "status": "resolved", "resolution": "", "version": 1})
    assert missing.status_code == 422
    processing = api.post(f"/api/tickets/{ticket_id}/update", data={"assigned_to": "售后主管·林悦", "status": "processing", "resolution": "", "version": 1})
    assert processing.status_code == 200
    resolved = api.post(f"/api/tickets/{ticket_id}/update", data={"assigned_to": "售后主管·林悦", "status": "resolved", "resolution": "已核验订单并按平台政策完成退款", "version": 2})
    assert resolved.status_code == 200
    assert resolved.json()["status"] == "resolved"
    assert "已核验订单" in api.get("/tickets").text


def test_auth_csrf_roles_and_audit_identity(client):
    api, main = client
    original_cookie = api.cookies.get("ops_session")
    original_csrf = api.headers["X-CSRF-Token"]
    api.cookies.clear()
    assert api.get("/dashboard", follow_redirects=False).status_code == 303
    assert api.get("/health/metrics", follow_redirects=False).status_code == 303
    api.cookies.set("ops_session", original_cookie)
    api.headers.pop("X-CSRF-Token")
    assert api.post("/api/products/1/analyze").status_code == 403

    with main.connect() as conn:
        conn.execute("INSERT INTO users(username,password_hash,role,is_active,created_at) VALUES(?,?,?,?,?)", ("viewer", main.hash_password("ViewerPassword123!"), "viewer", 1, main.now()))
        sid = "viewer-test-session"
        conn.execute("INSERT INTO user_sessions(session_id,username,created_at,expires_at,last_seen_at,user_agent,ip_masked) VALUES(?,?,?,?,?,?,?)", (sid, "viewer", main.now(), "2099-01-01T00:00:00+00:00", main.now(), "pytest", "test"))
        conn.commit()
    token, csrf = main.create_session("viewer", "viewer", main.SESSION_SECRET, session_id=sid)
    api.cookies.set("ops_session", token)
    api.headers["X-CSRF-Token"] = csrf
    assert api.post("/api/channels/amazon-us/sync/inventory").status_code == 403

    api.cookies.set("ops_session", original_cookie)
    api.headers["X-CSRF-Token"] = original_csrf
    upload(api, "/api/import/products", PRODUCT_CSV)
    with main.connect() as conn:
        row = conn.execute("SELECT actor,request_id FROM audit_events WHERE action='products_imported' ORDER BY id DESC LIMIT 1").fetchone()
        assert row["actor"] == "admin" and len(row["request_id"]) >= 12
        with pytest.raises(Exception):
            conn.execute("DELETE FROM audit_events WHERE id=(SELECT MAX(id) FROM audit_events)")


def test_optimistic_locking_and_ticket_state_machine(client):
    api, main = client
    upload(api, "/api/import/products", PRODUCT_CSV)
    verify_evidence(api, main, 1)
    listing = api.post("/api/products/1/listing/generate").json()
    payload = {"title": "Safe title", "bullet_points": "One\nTwo\nThree\nFour\nFive", "description": "Reviewed.", "search_terms": "safe, title", "version": 1}
    assert api.post(f"/api/listings/{listing['listing_id']}/approve", data=payload).status_code == 200
    assert api.post(f"/api/listings/{listing['listing_id']}/approve", data=payload).status_code == 409

    main.sync_ticket_rows([{"external_event_id": "state-1", "sku": "SKU-1", "event_type": "buyer_message", "source": "Messaging", "order_id_masked": "114-***-0002", "customer_alias": "客户 A***1", "event_at": "2026-09-03T01:00:00Z", "rating": 3, "refund_requested": False, "title": "Question", "message": "Please help"}])
    with main.connect() as conn:
        ticket_id = conn.execute("SELECT id FROM tickets WHERE external_event_id='state-1'").fetchone()[0]
    illegal = api.post(f"/api/tickets/{ticket_id}/update", data={"assigned_to": "客服", "status": "resolved", "resolution": "done", "version": 1})
    assert illegal.status_code == 409


def test_sync_failure_trace_and_customer_dlp(client, monkeypatch):
    api, main = client
    upload(api, "/api/import/products", PRODUCT_CSV)
    result = main.sync_inventory_rows([{"external_event_id": "bad", "sku": "SKU-1", "fulfillable": -1, "reserved": 0, "inbound": 0, "unfulfillable": 0, "daily_sales": 1, "lead_time_days": 2}])
    assert result["errors"] == 1
    with main.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM sync_failures WHERE sync_run_id=?", (result["sync_run_id"],)).fetchone()[0] == 1

    main.sync_ticket_rows([{"external_event_id": "dlp-1", "sku": "SKU-1", "event_type": "buyer_message", "source": "Messaging", "order_id_masked": "114-***-0003", "customer_alias": "客户 B***1", "event_at": "2026-09-03T01:00:00Z", "rating": 3, "refund_requested": "false", "title": "Contact", "message": "Email me at buyer@example.com or 415-555-1212"}])
    with main.connect() as conn:
        ticket_id = conn.execute("SELECT id FROM tickets WHERE external_event_id='dlp-1'").fetchone()[0]
    captured = {}
    def fake_ai(ticket):
        captured.update(ticket)
        return "Hello", "人工核验", "demo", "demo", {"latency_ms": 0, "input_tokens": 1, "output_tokens": 1, "estimated_cost_usd": 0}
    monkeypatch.setattr(main, "call_ticket_ai", fake_ai)
    assert api.post(f"/api/tickets/{ticket_id}/draft-reply").status_code == 200
    assert "buyer@example.com" not in captured["message"] and "415-555-1212" not in captured["message"]
    assert "customer_alias" not in captured and "order_id_masked" not in captured


def test_durable_sync_job_and_health(client):
    api, main = client
    upload(api, "/api/import/products", PRODUCT_CSV)
    result = api.post("/api/channels/amazon-us/sync/inventory").json()
    assert result["job_status"] == "succeeded"
    job = api.get(f"/api/jobs/{result['job_id']}").json()
    assert job["status"] == "succeeded" and job["attempts"] == 1
    assert api.get("/health/live").status_code == 200
    ready = api.get("/health/ready")
    assert ready.status_code == 200 and ready.json()["schema_version"] >= 2


def test_production_config_fails_closed(client, monkeypatch):
    _, main = client
    monkeypatch.setattr(main, "APP_ENV", "production")
    monkeypatch.setattr(main, "SESSION_SECRET", "replace-with-a-random-secret-at-least-32-characters")
    monkeypatch.setenv("ADMIN_PASSWORD", "replace-with-a-strong-password")
    monkeypatch.delenv("AI_API_KEY", raising=False)
    monkeypatch.setenv("ALLOW_DEMO_AI", "false")
    with pytest.raises(RuntimeError, match="生产配置校验失败"):
        main.validate_runtime_config()


def test_each_sample_product_gets_distinct_bilingual_copy(client):
    _, main = client
    sample_path = Path(__file__).resolve().parent.parent / "samples" / "products.csv"
    with sample_path.open(encoding="utf-8-sig", newline="") as handle:
        copies = [main.validate_listing(main.demo_listing(row)) for row in csv.DictReader(handle)]
    assert len({copy["description"] for copy in copies}) == len(copies)
    assert len({copy["description_zh"] for copy in copies}) == len(copies)
    assert len({tuple(copy["bullet_points"]) for copy in copies}) == len(copies)
    assert len({tuple(copy["bullet_points_zh"]) for copy in copies}) == len(copies)


def test_feedback_actions_and_private_ticket_attachments(client):
    api, main = client
    samples = Path(__file__).resolve().parent.parent / "samples"
    upload(api, "/api/import/products", (samples / "products.csv").read_bytes())
    api.post("/api/channels/amazon-us/sync/orders")
    api.post("/api/channels/amazon-us/sync/tickets")
    api.post("/api/channels/amazon-us/sync/feedback")
    event_id = "CF-US-B0D8H4P7X3-2026-08"
    created = api.post(
        f"/api/feedback/{event_id}/actions",
        data={"action_type": "product_quality", "owner": "质量负责人", "due_at": "2099-01-01T12:00"},
    )
    assert created.status_code == 200
    action_id = created.json()["action_id"]
    assert api.post(f"/api/feedback-actions/{action_id}/update", data={"status": "resolved", "outcome": "", "version": 1}).status_code == 422
    completed = api.post(
        f"/api/feedback-actions/{action_id}/update",
        data={"status": "resolved", "outcome": "供应商已完成电池批次整改", "version": 1},
    )
    assert completed.status_code == 200 and completed.json()["status"] == "resolved"
    assert "供应商已完成电池批次整改" in api.get("/feedback").text

    with main.connect() as conn:
        ticket_id = conn.execute("SELECT id FROM tickets WHERE is_archived=0 LIMIT 1").fetchone()[0]
    png = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=")
    attachment = api.post(
        f"/api/tickets/{ticket_id}/attachments",
        files={"file": ("buyer-photo.png", io.BytesIO(png), "image/png")},
    )
    assert attachment.status_code == 200
    attachment_id = attachment.json()["attachment_id"]
    downloaded = api.get(f"/api/tickets/{ticket_id}/attachments/{attachment_id}")
    assert downloaded.status_code == 200 and downloaded.content == png
    stored_file = next(main.PRIVATE_ROOT.iterdir())
    assert not (main.MEDIA_ROOT / stored_file.name).exists()
    bad = api.post(
        f"/api/tickets/{ticket_id}/attachments",
        files={"file": ("fake.png", io.BytesIO(b"not a png"), "image/png")},
    )
    assert bad.status_code == 422


def test_account_password_mfa_users_and_session_revocation(client):
    api, main = client
    assert api.get("/settings").status_code == 200
    created = api.post("/api/admin/users", data={"username": "viewer1", "password": "ViewerPass1234", "role": "viewer"})
    assert created.status_code == 200 and created.json()["must_change_password"] is True
    setup = api.post("/api/account/mfa/setup").json()
    with main.connect() as conn:
        assert conn.execute("SELECT mfa_secret FROM users WHERE username='admin'").fetchone()[0].startswith("gAAAA")
    assert api.post("/api/account/mfa/enable", data={"code": main.totp_code(setup["secret"])}).status_code == 200
    changed = api.post(
        "/api/account/password",
        data={"current_password": "TestAdmin123!ChangeMe", "new_password": "NewAdminPass5678"},
    )
    assert changed.status_code == 200
    api.post("/logout")
    missing_otp = api.post("/login", data={"username": "admin", "password": "NewAdminPass5678"}, follow_redirects=False)
    assert missing_otp.status_code == 303 and "error=otp" in missing_otp.headers["location"]
    for _ in range(4):
        missing_otp = api.post("/login", data={"username": "admin", "password": "NewAdminPass5678", "otp": "000000"}, follow_redirects=False)
    assert "error=locked" in missing_otp.headers["location"]
    with main.connect() as conn:
        conn.execute("DELETE FROM login_attempts")
        conn.commit()
    login = api.post("/login", data={"username": "admin", "password": "NewAdminPass5678", "otp": main.totp_code(setup["secret"])}, follow_redirects=False)
    assert login.status_code == 303


def test_audit_health_metrics_and_backup_restore(client, tmp_path):
    api, main = client
    upload(api, "/api/import/products", PRODUCT_CSV)
    health = api.get("/health").json()
    assert health["audit_chain"] == "ok" and health["database_integrity"] == "ok" and health["schema_version"] == 18
    metrics = api.get("/health/metrics")
    assert metrics.status_code == 200 and "tickets_overdue" in metrics.json()
    from scripts.backup_db import backup
    from scripts.check_migrations import check
    from scripts.restore_db import restore
    backup_file = backup(main.db_path(), tmp_path / "backups", retention=2)
    assert backup_file.with_suffix(".json").is_file()
    restored = restore(backup_file, tmp_path / "restored" / "app.db")
    assert check(restored) == 18


def test_sql_pagination_notifications_and_real_concurrent_review(client):
    api, main = client
    header = "sku,asin,source_title,category,price,cost,monthly_sales,rating,review_count,competition_score,stock_units,daily_sales,lead_time_days\n"
    rows = [f"LOAD-{i:03d},B0LOAD{i:06d},Scale {i},kitchen,20,8,100,4.2,30,50,10,2,7" for i in range(35)]
    assert upload(api, "/api/import/products", (header + "\n".join(rows)).encode()).json()["imported"] == 35
    first_page = main.page_context("products", {"page_no": "1"})
    second_page = main.page_context("products", {"page_no": "2"})
    assert first_page["pagination"]["total"] == 35 and len(first_page["products"]) == 10
    assert {p["id"] for p in first_page["products"]}.isdisjoint({p["id"] for p in second_page["products"]})

    main.sync_ticket_rows([{
        "external_event_id": "overdue-notification", "sku": "LOAD-000", "event_type": "return_refund",
        "source": "演示退货报告", "order_id_masked": "111-***-9999", "customer_alias": "客户 A***1",
        "event_at": "2020-01-01T00:00:00Z", "rating": 1, "refund_requested": True,
        "title": "逾期退货", "message": "Item failed",
    }])
    dashboard = api.get("/dashboard")
    assert dashboard.status_code == 200 and "已超过 SLA" in dashboard.text
    with main.connect() as conn:
        notification_id = conn.execute("SELECT id FROM operational_notifications WHERE status='unread' LIMIT 1").fetchone()[0]
    assert api.post(f"/api/notifications/{notification_id}/read").json()["status"] == "read"

    verify_evidence(api, main, 1)
    listing = api.post("/api/products/1/listing/generate").json()
    def approve_once(_):
        return api.post(f"/api/listings/{listing['listing_id']}/approve", data={"version": 1}).status_code
    with ThreadPoolExecutor(max_workers=2) as pool:
        statuses = sorted(pool.map(approve_once, range(2)))
    assert statuses == [200, 409]
