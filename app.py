import json
import os
import streamlit as st

import config
import engine
import payments
import db

config.load_environment()
from engine import (
    parse_rfq_with_gemini, fetch_all_live_suppliers, get_reference_fallback_quotes,
    optimize_supply_chain, execute_supplier_procurement, revalidate_mandate_before_purchase,
    check_environment, get_audit_log, _detect_product_category,
    SPEND_MANDATE, SUPPLIER_SOURCES,
)

st.set_page_config(page_title="Demand2Deal | Autonomous Distributor", page_icon="⚡", layout="wide")

# Simple top-level view selector
view = st.sidebar.selectbox("View", ["App", "History"], index=0)
if view == "History":
    st.set_page_config(page_title="Demand2Deal | History", page_icon="📜", layout="wide")
    st.title("📜 Purchase & Payment History")
    st.markdown("A polished snapshot of the latest supplier purchases and payment verifications captured by the demo flow.")

    purchases = db.get_purchases(200)
    payments_list = db.get_payments(200)

    col1, col2, col3 = st.columns([1.2, 1.2, 1.2])
    col1.metric("Total Purchases", len(purchases), help="Successful and attempted supplier orders")
    col2.metric("Total Payments", len(payments_list), help="Verified and simulated payment events")
    col3.metric("Storage", "SQLite", help="Local history database")

    st.markdown("---")
    st.subheader("🛒 Purchases")
    if purchases:
        purchase_rows = []
        for row in purchases:
            details = json.loads(row.get("details", "{}") or "{}")
            purchase_rows.append({
                "ID": row.get("id"),
                "Product": row.get("product", "—"),
                "Supplier": row.get("supplier", "—"),
                "Qty": row.get("quantity", 0),
                "Status": row.get("status", "UNKNOWN"),
                "Created": row.get("created_at", "—")[:19],
                "Correlation": details.get("correlation_id", "—"),
            })
        st.dataframe(purchase_rows, use_container_width=True, hide_index=True)
    else:
        st.info("No purchases recorded yet.")

    st.subheader("💳 Payments")
    if payments_list:
        payment_rows = []
        for row in payments_list:
            details = json.loads(row.get("details", "{}") or "{}")
            payment_rows.append({
                "ID": row.get("id"),
                "Order ID": row.get("razorpay_order_id", "—"),
                "Payment ID": row.get("razorpay_payment_id", "—"),
                "Verified": "✅ Yes" if row.get("verified") else "❌ No",
                "Created": row.get("created_at", "—")[:19],
                "Correlation": details.get("correlation_id", "—"),
            })
        st.dataframe(payment_rows, use_container_width=True, hide_index=True)
    else:
        st.info("No payments recorded yet.")

    st.markdown("---")
    c1, c2, c3 = st.columns([1, 1, 1.2])
    c1.download_button("Download purchases (JSON)", data=str(purchases), file_name="purchases.json")
    c2.download_button("Download payments (JSON)", data=str(payments_list), file_name="payments.json")
    if c3.button("🗑️ Clear history"):
        db.clear_history()
        st.success("History cleared.")
        st.rerun()

    st.stop()

st.markdown("""
<style>
    .money-shot {
        background: linear-gradient(135deg, #065F46 0%, #047857 100%);
        color: white; padding: 25px; border-radius: 12px;
        box-shadow: 0 4px 15px rgba(0,0,0,0.3); margin-top: 20px;
    }
    .badge-live { background:#DCFCE7; color:#166534; padding:2px 8px; border-radius:6px; font-size:0.75rem; font-weight:600; }
    .badge-reference { background:#FEF3C7; color:#92400E; padding:2px 8px; border-radius:6px; font-size:0.75rem; font-weight:600; }
    .badge-web { background:#DBEAFE; color:#1E40AF; padding:2px 8px; border-radius:6px; font-size:0.75rem; font-weight:600; }
    .mandate-pass { color:#166534; }
    .mandate-fail { color:#B91C1C; }
    .audit-event { padding: 8px; border-left: 3px solid #065F46; margin: 4px 0; background: #F9FAFB; }
</style>
""", unsafe_allow_html=True)

# ---------------------------------------------------------------------------
# Session state
# ---------------------------------------------------------------------------
def reset_workflow() -> None:
    defaults = {
        "step": "INPUT",
        "demand": None,
        "suppliers": None,
        "plan": None,
        "data_source": "live",
        "razorpay_order": None,
        "payment_verified": False,
        "procurement_result": None,
        "payment_note": "",
        "rzp_processed_ids": set(),
        "override_feasibility": False,
        "prompt_text": "",
    }
    for key, default in defaults.items():
        st.session_state[key] = default

for key, default in [
    ("step", "INPUT"), ("demand", None), ("suppliers", None), ("plan", None),
    ("data_source", "live"), ("razorpay_order", None), ("payment_verified", False),
    ("procurement_result", None), ("payment_note", ""), ("rzp_processed_ids", set()),
    ("override_feasibility", False), ("prompt_text", ""),
]:
    if key not in st.session_state:
        st.session_state[key] = default

# ---------------------------------------------------------------------------
# Catch the Razorpay Checkout redirect BEFORE anything else renders.
# ---------------------------------------------------------------------------
qp = st.query_params
if "rzp_payment_id" in qp and qp["rzp_payment_id"] not in st.session_state.rzp_processed_ids:
    pid = qp["rzp_payment_id"]
    verified = payments.verify_payment(qp.get("rzp_order_id", ""), pid, qp.get("rzp_signature", ""))
    st.session_state.rzp_processed_ids.add(pid)
    st.query_params.clear()
    if verified:
        st.session_state.payment_verified = True
        st.session_state.step = "PROCURING"
    else:
        st.session_state.payment_error = "Signature verification failed — this payment could not be confirmed as genuine."
    st.rerun()

st.title("⚡ Demand2Deal — The Autonomous Distributor")
st.caption("Commerce without Inventory | webcmd-powered · Web-wide supplier discovery · Real checkout automation")

st.markdown("""
<div style="padding: 18px 20px; border-radius: 14px; background: linear-gradient(135deg, #0f172a, #1d4ed8); color: white; margin-bottom: 18px;">
  <strong>Launch a smarter RFQ flow</strong><br>
  Describe what your customer needs, pick suppliers, and let the agent discover and compare options in one guided workflow.
</div>
""", unsafe_allow_html=True)

# ---------------------------------------------------------------------------
# Startup diagnostics
# ---------------------------------------------------------------------------
env_checks = check_environment()
broken = [c for c in env_checks if not c["ok"] and c["name"] != "Razorpay"]
if broken:
    with st.expander("⚠️ Setup needed for a fully live run", expanded=True):
        for c in broken:
            st.warning(f"**{c['name']}**: {c['detail']}")

# ---------------------------------------------------------------------------
# Sidebar: Agent Spending Mandate
# ---------------------------------------------------------------------------
st.sidebar.header("🛡️ Agent Spending Mandate")
if st.sidebar.button("🧹 Start new RFQ", use_container_width=True):
    reset_workflow()
    st.rerun()
st.sidebar.metric("Max Order Ceiling", f"₹{SPEND_MANDATE['max_order_spend']:,.0f}")
st.sidebar.metric("Min Gross Margin", f"{SPEND_MANDATE['min_gross_margin']:.0%}")
st.sidebar.metric("Max Price Movement", f"{SPEND_MANDATE['max_price_movement_pct']:.0%}")
st.sidebar.metric("Risk Buffer", f"{SPEND_MANDATE['risk_buffer_pct']:.0%}")
st.sidebar.write("**Substitution Policy:**", SPEND_MANDATE["substitution_policy"].replace("_", " ").title())
st.sidebar.write("**Approved merchants:**", ", ".join(SPEND_MANDATE["allowed_merchants"]))
live_names = ", ".join(s["name"] for s in SUPPLIER_SOURCES)
st.sidebar.caption(f"Known distributors: {live_names}")
st.sidebar.caption("➕ Web-wide discovery enabled — finds suppliers across the open web")
if not payments.is_configured():
    st.sidebar.warning("Razorpay not configured — payment step will run in **simulated** mode.")
elif payments.is_live_keys():
    st.sidebar.error("⚠️ LIVE Razorpay keys detected, not Test Mode.")
else:
    st.sidebar.success("Razorpay Test Mode connected.")

# ---------------------------------------------------------------------------
# Step 1: Human Prompt Input
# ---------------------------------------------------------------------------
st.subheader("1. Describe the customer request")
st.info("Enter the request in plain English. The more specific the request, the better the agent can rank suppliers.")

example_prompts = {
    "Mobile": "Need 5 iPhone 17 units delivered in Bengaluru within 3 days. Maximum customer price ₹10,000 each. Minimum margin 8%.",
    "Electronics": "Need 8 Raspberry Pi 5 8GB units delivered in Mumbai within 4 days. Maximum customer price ₹9,500 each. Minimum margin 10%.",
    "Accessories": "Need 12 Logitech MX Keys Mini keyboards delivered in Delhi within 2 days. Maximum customer price ₹7,000 each. Minimum margin 9%.",
}
example_cols = st.columns(3)
for col, (label, example) in zip(example_cols, example_prompts.items()):
    if col.button(f"✨ {label}", use_container_width=True):
        st.session_state.prompt_text = example
        st.rerun()

prompt = st.text_area("Your RFQ prompt", key="prompt_text", placeholder="Type the customer request here…", height=95)

# Supplier selection widget
st.markdown("**2. Choose suppliers to query**")
st.caption("Fewer suppliers usually means faster results.")
all_supplier_options = {s["supplier_id"]: s["name"] for s in SUPPLIER_SOURCES}
all_supplier_options["web_discovery"] = "Open Web Discovery (webcmd search)"
webcmd_supercharge = st.checkbox(
    "Maximize webcmd usage: query all supported suppliers + open web discovery",
    value=True,
    help="This is the fullest webcmd-heavy path and the strongest demo case for the hackathon.",
)
if webcmd_supercharge:
    default_suppliers = list(all_supplier_options.keys())
else:
    default_suppliers = ["amazon_in", "flipkart", "web_discovery"]
selected_suppliers = st.multiselect(
    "Choose suppliers:",
    options=list(all_supplier_options.keys()),
    default=default_suppliers,
    format_func=lambda x: all_supplier_options[x],
    help="Only selected suppliers will be queried. Amazon.in + Flipkart + web discovery is a webcmd-first flow.",
)

if st.button("🚀 Run discovery & optimize", type="primary"):
    if not prompt.strip():
        st.warning("Enter what the customer needs first.")
    else:
        with st.status("Executing Agentic Discovery & Optimization...", expanded=True) as status:
            st.write("1. 🧠 Gemini parsing RFQ requirements...")
            try:
                demand = parse_rfq_with_gemini(prompt)
            except Exception as ex:
                status.update(label=f"❌ RFQ parsing failed: {ex}", state="error")
                st.stop()

            st.write("2. 🌐 Searching selected suppliers via `webcmd`...")
            for sid in selected_suppliers:
                st.write(f"   • {all_supplier_options.get(sid, sid)}")
            if "web_discovery" in selected_suppliers:
                st.write("   • Also searching the web for additional suppliers...")
            suppliers = fetch_all_live_suppliers(demand, selected_supplier_ids=selected_suppliers)
            st.session_state.data_source = "live"

            if not suppliers:
                status.update(label="⚠️ Live search returned no results.", state="error")
                st.session_state.demand = demand
                st.session_state.suppliers = []
                st.session_state.plan = None
                st.session_state.step = "NO_RESULTS"
            else:
                st.write("3. 📊 Running commercial optimization (MOQ, compatibility, risk buffer, substitution)...")
                plan = optimize_supply_chain(demand, suppliers)
                st.session_state.demand = demand
                st.session_state.suppliers = suppliers
                st.session_state.plan = plan
                st.session_state.step = "OPTIMIZED"
                status.update(label="✅ Supplier Discovery & Optimization Complete!", state="complete")

# ---------------------------------------------------------------------------
# Honest fallback: live search came back empty.
# ---------------------------------------------------------------------------
if st.session_state.step == "NO_RESULTS":
    st.error(
        "No matching suppliers came back from live search. The app will not invent reference fallback rows automatically. "
        "Try a different product term, a broader supplier set, or use reference pricing only if you explicitly want to continue the demo."
    )
    if st.button("📋 Continue with reference pricing", type="secondary"):
        demand = st.session_state.demand
        suppliers = get_reference_fallback_quotes(demand)
        plan = optimize_supply_chain(demand, suppliers)
        st.session_state.suppliers = suppliers
        st.session_state.plan = plan
        st.session_state.data_source = "reference"
        st.session_state.step = "OPTIMIZED"
        st.rerun()

# ---------------------------------------------------------------------------
# Step 2: Display Supplier Comparison
# ---------------------------------------------------------------------------
if st.session_state.step in ["OPTIMIZED", "PROCURING", "PAID"]:
    demand = st.session_state.demand
    suppliers = st.session_state.suppliers
    plan = st.session_state.plan

    if demand is None or suppliers is None or plan is None:
        st.warning("The RFQ context is incomplete. Please start a new request and complete the optimization flow.")
        st.stop()

    st.markdown("---")
    phase_badge = "<span class=\"badge-live\">● LIVE RUN</span>" if st.session_state.step in ["OPTIMIZED", "PROCURING", "PAID"] else "<span class=\"badge-reference\">◐ READY TO RUN</span>"
    st.markdown(
        f"<div style='display:flex; gap:8px; flex-wrap:wrap; align-items:center; margin-bottom:10px;'>{phase_badge}<span class='badge-web'>⚙️ Guided RFQ workflow</span></div>",
        unsafe_allow_html=True,
    )
    badge = (
        '<span class="badge-live">● LIVE DATA</span>' if st.session_state.data_source == "live"
        else '<span class="badge-reference">◐ REFERENCE DATA (not live)</span>'
    )
    st.markdown(
        f"### 2. Supplier Search Results: {demand.product} (Target: {demand.target_qty} units) {badge}",
        unsafe_allow_html=True,
    )

    # Summary stats
    known_count = len([s for s in suppliers if s.source in ("live", "reference")])
    web_count = len([s for s in suppliers if s.source == "web_discovered"])
    if web_count > 0:
        st.caption(f"🔍 {known_count} known distributors + {web_count} web-discovered suppliers")

    # Show substitution info
    if demand.substitution_allowed:
        st.info("✅ Substitutions approved by customer")
    else:
        st.caption("ℹ️ Substitutions not requested — exact matches preferred")

    if not suppliers:
        st.error("No matching suppliers found. Try adjusting the product term or delivery timeline.")
    else:
        # Supplier table with columns
        cols = st.columns(7)
        st.caption("Tap a supplier row to review its fit against the mandate and commercial constraints.")
        for c, label in zip(cols, ["**Supplier**", "**Stock**", "**Unit Cost**", "**MOQ**", "**Delivery**", "**Compat**", "**Agent Decision**"]):
            c.write(label)

        for s in suppliers:
            c = st.columns(7)
            # Source badge + rating
            name_html = s.name
            if s.source == "web_discovered":
                name_html += ' <span class="badge-web">🔍 WEB</span>'
            if s.rating > 0:
                name_html += f' <span style="font-size:0.8rem;color:#F59E0B;">⭐{s.rating:.1f}</span>'
                if s.review_count > 0:
                    name_html += f' <span style="font-size:0.7rem;color:#666;">({s.review_count} reviews)</span>'
            c[0].markdown(name_html, unsafe_allow_html=True)
            if s.product_title:
                c[0].caption(f"*{s.product_title[:60]}{'...' if len(s.product_title) > 60 else ''}*")

            stock_txt = f"{s.stock} units" + (" *(est.)*" if s.is_estimate.get("stock") else "")
            c[1].write(stock_txt)
            c[2].write(f"₹{s.unit_cost:,.2f}" if s.unit_cost > 0 else "—")
            c[3].write(f"MOQ: {s.moq}")
            lead_txt = f"{s.lead_time_days} days" + (" *(est.)*" if s.is_estimate.get("lead_time_days") else "")
            c[4].write(lead_txt)

            # Compatibility
            if demand.compatibility_required:
                if s.compatibility_score >= 0.8:
                    c[5].success("✅")
                elif s.compatibility_score >= 0.5:
                    c[5].warning("⚠️")
                else:
                    c[5].error("❌")
            else:
                c[5].write("—")

            # Decision
            if s.lead_time_days > demand.max_delivery_days:
                c[6].error(f"❌ Reject: SLA")
            elif s.moq > demand.target_qty:
                c[6].error(f"❌ Reject: MOQ")
            elif plan and s.supplier_id in plan.supplier_allocations:
                c[6].success(f"✅ Selected ({plan.supplier_allocations[s.supplier_id]} units)")
            else:
                c[6].info("⏸️ Backup")

        if plan and not plan.is_feasible:
            st.warning(f"⚠️ **Not feasible:** {plan.rejection_reason}")
            if plan.compatibility_issues:
                with st.expander("Compatibility issues"):
                    for issue in plan.compatibility_issues:
                        st.write(f"• {issue}")
            if plan.substitution_used:
                st.info("ℹ️ Substitution would have been required — check substitution policy")
            # Override option: let the user proceed despite the feasibility check
            if st.button("⚠️ Proceed anyway (override mandate check)", type="secondary"):
                st.session_state.override_feasibility = True
                st.rerun()

        # -------------------------------------------------------------
        # Step 3: Customer Quote & Payment
        # -------------------------------------------------------------
        if plan and (plan.is_feasible or st.session_state.get("override_feasibility", False)):
            st.markdown("---")
            st.subheader("3. Customer Quote & Commercial Loop")
            q1, q2, q3, q4 = st.columns(4)
            q1.metric("Proposed Selling Price", f"₹{plan.total_revenue:,.2f}")
            q2.metric("Expected Supplier Cost", f"₹{plan.total_cost:,.2f}")
            q3.metric("Risk Buffer", f"{plan.risk_buffer_pct:.1%}")
            q4.metric("Expected Gross Profit", f"₹{plan.gross_profit:,.2f} ({plan.margin_pct:.1%})")

            if plan.substitution_used:
                st.info("ℹ️ This quote includes substitutions (not all products are exact matches)")

            if st.session_state.step == "OPTIMIZED":
                if st.session_state.get("simulated_payment") is None:
                    st.session_state.simulated_payment = engine.simulate_payment_flow(demand, plan)
                st.markdown("### 4. Customer quote & payment")

                payment = st.session_state.simulated_payment
                invoice = payment["invoice"]

                st.markdown("#### 💳 Simulated Customer Invoice & Payment")
                st.caption("This hackathon demo uses a complete simulated invoice flow for customer payment authorization.")

                inv_cols = st.columns([2, 2, 2, 2])
                inv_cols[0].markdown(f"**Invoice #** {invoice['invoice_number']}")
                inv_cols[1].markdown(f"**Date**\n{invoice['date']}")
                inv_cols[2].markdown(f"**Status**\n{invoice['status']}")
                inv_cols[3].markdown(f"**Total**\n₹{invoice['total_amount']:,.2f}")

                st.markdown("**Billed to:**")
                st.write(f"{invoice['billed_to']['name']} — {invoice['billed_to']['location']}")
                st.markdown("**Sold by:**")
                st.write(f"{invoice['sold_by']['name']} — {invoice['sold_by']['contact']}")

                item_rows = [
                    {"Description": item["description"], "Qty": item["quantity"], "Unit Price": f"₹{item['unit_price']:,.2f}", "Total": f"₹{item['total']:,.2f}"}
                    for item in invoice["items"]
                ]
                st.table(item_rows)
                st.caption("This is a demo invoice for a simulated customer payment authorization; no actual funds are transferred.")

                if st.button("✅ Simulate Customer Payment and proceed to supplier checkout", type="primary"):
                    try:
                        db.record_payment(
                            None,
                            None,
                            None,
                            True,
                            details={
                                "mode": "simulated",
                                "invoice_number": invoice["invoice_number"],
                                "note": f"Simulated payment for {invoice['invoice_number']}",
                                "correlation_id": f"sim-{invoice['invoice_number']}",
                            },
                        )
                    except Exception:
                        pass
                    st.session_state.payment_verified = True
                    st.session_state.step = "PROCURING"
                    st.session_state.payment_note = f"Simulated invoice {invoice['invoice_number']} paid."
                    st.session_state.payment_flow = payment
                    st.rerun()

            if st.session_state.payment_verified and st.session_state.step == "PROCURING":
                st.success("Simulated customer payment captured — proceeding to supplier checkout via webcmd browser automation.")

# ---------------------------------------------------------------------------
# Procurement: re-validate the mandate, then execute webcmd checkout
# ---------------------------------------------------------------------------
if st.session_state.step == "PROCURING":
    demand, plan, suppliers = st.session_state.demand, st.session_state.plan, st.session_state.suppliers
    with st.status("Completing Commercial Loop — webcmd Supplier Checkout...", expanded=True) as status:
        st.write("1. 💳 Customer payment validated ✅")
        st.write("2. 🛡️ Re-checking mandate: allowlist, spend ceiling, margin, SLA, price drift, stock, substitution...")
        st.write("3. 🤖 Driving webcmd browser checkout automation for each supplier...")
        result = execute_supplier_procurement(demand, plan, suppliers, override_checks=st.session_state.get("override_feasibility", False))
        st.session_state.procurement_result = result

        try:
            if result.get("orders"):
                for order in result.get("orders", []):
                    db.record_purchase(
                        product=demand.product,
                        quantity=order.get("quantity", demand.target_qty),
                        supplier=order.get("supplier") or order.get("supplier_name") or order.get("name") or "",
                        product_url=order.get("product_url", ""),
                        status=order.get("status", result.get("status", "UNKNOWN")),
                        details={
                            "note": order.get("note", ""),
                            "correlation_id": f"proc-{demand.product}-{order.get('supplier','')}-{order.get('quantity', demand.target_qty)}",
                        },
                    )
            else:
                db.record_purchase(
                    product=demand.product,
                    quantity=demand.target_qty,
                    supplier="",
                    product_url="",
                    status=result.get("status", "UNKNOWN"),
                    details={
                        "note": "Summary procurement record created from app flow",
                        "correlation_id": f"proc-{demand.product}-{demand.target_qty}",
                    },
                )
        except Exception:
            pass

        if result["status"] == "BLOCKED_BY_MANDATE":
            status.update(label="🛑 Blocked by Agent Spending Mandate", state="error")
        else:
            for step_info in result.get("payment_flow", {}).get("steps", []):
                st.write(f"   • {step_info.get('action', 'Step')} — {step_info.get('status', 'unknown')}")
            for o in result.get("orders", []):
                supplier_name = o.get("supplier") or o.get("supplier_name") or o.get("name") or "Unknown supplier"
                quantity = o.get("quantity", 1)
                status_value = o.get("status", "UNKNOWN")
                st.write(f"   • {supplier_name}: {quantity} units → {status_value}")
                for s in o.get("steps", []):
                    st.write(f"     - {s.get('action', 'Step')} — {s.get('status', 'unknown')}")
            status.update(label="🎉 Order Sourced & Fulfilled!", state="complete")

    st.session_state.step = "PAID"
    st.rerun()

# ---------------------------------------------------------------------------
# Step 4: Result — mandate outcome + (if passed) the Money Shot
# ---------------------------------------------------------------------------
if st.session_state.step == "PAID":
    plan = st.session_state.plan
    result = st.session_state.procurement_result or {}
    if plan is None:
        st.warning("The optimization plan is no longer available. Please start a new RFQ to rebuild the demo flow.")
        st.stop()

    with st.expander("🛡️ Mandate re-check detail (Section 5.1)", expanded=(result.get("status") == "BLOCKED_BY_MANDATE")):
        for c in result.get("mandate", {}).get("checks", []):
            icon = "✅" if c["passed"] else "❌"
            css = "mandate-pass" if c["passed"] else "mandate-fail"
            st.markdown(f'{icon} <span class="{css}">**{c["name"]}**</span> — {c["detail"]}', unsafe_allow_html=True)

    if result.get("status") == "BLOCKED_BY_MANDATE":
        st.error(
            "🛑 The agent's own spending mandate blocked this purchase after the final "
            "pre-payment re-check — see detail above. The customer payment was collected "
            "but supplier procurement did not proceed."
        )
    else:
        order_lines = "".join(
            f'<div style="margin-top:6px;font-size:0.95rem;">• {o.get("supplier") or o.get("supplier_name") or o.get("name") or "Unknown supplier"}: {o.get("quantity", 1)} units — '
            f'<strong>{str(o.get("status", "UNKNOWN")).replace("_", " ")}</strong></div>'
            for o in result.get("orders", [])
        )
        badge = (
            '<span class="badge-live" style="background:rgba(255,255,255,0.25);color:white;">LIVE DATA</span>'
            if st.session_state.data_source == "live"
            else '<span class="badge-reference" style="background:rgba(255,255,255,0.25);color:white;">REFERENCE DATA</span>'
        )
        st.markdown(f"""
        <div class="money-shot">
            <h2>🏆 SALE COMPLETE — THE MONEY SHOT {badge}</h2>
            <hr style="border-color: rgba(255,255,255,0.2);">
            <div style="display: flex; justify-content: space-around; font-size: 1.2rem;">
                <div><strong>Customer Revenue:</strong><br> ₹{plan.total_revenue:,.2f}</div>
                <div><strong>Supplier Cost:</strong><br> ₹{plan.total_cost:,.2f}</div>
                <div><strong>Risk Buffer:</strong><br> {plan.risk_buffer_pct:.1%}</div>
                <div><strong>Gross Profit:</strong><br> ₹{plan.gross_profit:,.2f} ({plan.margin_pct:.1%})</div>
            </div>
            <hr style="border-color: rgba(255,255,255,0.2);">
            <div style="display: flex; justify-content: space-around; font-size: 1rem;">
                <div><strong>Inventory Owned Before:</strong> 0</div>
                <div><strong>Human Procurement Actions:</strong> 0</div>
                <div><strong>Fulfilled via:</strong> webcmd Browser Automation</div>
            </div>
            {order_lines}
        </div>
        """, unsafe_allow_html=True)

        # Checkout steps detail
        if result.get("orders"):
            with st.expander("📋 webcmd Checkout Automation Detail", expanded=False):
                for o in result["orders"]:
                    st.markdown(f"**{o['supplier']}** ({o['quantity']} units)")
                    for s in o.get("steps", []):
                        icon = "✅" if s["status"] == "completed" else ("⚠️" if s["status"] == "warning" else "ℹ️")
                        st.write(f"  {icon} {s['action']} — {s['status']}")
                    st.write(f"**Result:** {o['status']}")
                    if o.get("note"):
                        st.caption(f"Note: {o['note']}")
                    st.divider()

    # Audit trail
    audit_log = get_audit_log()
    if audit_log:
        with st.expander("📜 Audit Trail", expanded=False):
            for event in audit_log[-10:]:  # Show last 10 events
                icon = "✅" if event["status"] == "success" else ("⚠️" if event["status"] == "warning" else "❌")
                st.markdown(
                    f'<div class="audit-event">{icon} <strong>{event["event_type"]}</strong> '
                    f'<span style="color:#666;font-size:0.85rem;">{event["timestamp"]}</span></div>',
                    unsafe_allow_html=True,
                )

    if st.button("🔁 Start a new RFQ"):
        for key in ["step", "demand", "suppliers", "plan", "razorpay_order", "payment_verified", "procurement_result", "override_feasibility"]:
            st.session_state[key] = {"step": "INPUT", "override_feasibility": False}.get(key, None)
        st.session_state.step = "INPUT"
        st.rerun()
