# Ameen Digital AI Workforce — Operational Runbook

This runbook guides operators and supervisors in running, testing, and managing the Ameen Digital AI Workforce.

---

## 1. Local Environment Setup & Verification

### Running Automated Test Suite:
```bash
# from the repository root
pip install -r requirements.txt
python -m pytest -q
```

Expected output: `68 passed` (workforce engine, HITL escalation, gateway policy guard,
LLM router, SOUL/router client, and the deterministic GTM container linter).

---

## 2. Launching the Local Workforce Service

```powershell
python -m uvicorn src.ameen_workforce.service:app --host 127.0.0.1 --port 58770 --reload
```

Endpoints available:
- Health check: `http://127.0.0.1:58770/health`
- Root info: `http://127.0.0.1:58770/`
- Pending Escalations: `http://127.0.0.1:58770/escalations`
- API Docs: `http://127.0.0.1:58770/docs`

---

## 3. Resolving a Human-in-the-Loop Escalation

When an AI Employee halts a task requiring payment verification or confidential approval:

1. **View Pending Escalations:**
   ```powershell
   curl.exe http://127.0.0.1:58770/escalations
   ```
2. **Approve Escalation:**
   ```powershell
   curl.exe -X POST http://127.0.0.1:58770/escalations/{ESCALATION_ID}/resolve `
     -H "Content-Type: application/json" `
     -d '{\"approved\": true, \"supervisor_id\": \"SUP-OPS-1\", \"note\": \"Verified payment and NDA authorized\"}'
   ```
3. **Reject Escalation:**
   ```powershell
   curl.exe -X POST http://127.0.0.1:58770/escalations/{ESCALATION_ID}/resolve `
     -H "Content-Type: application/json" `
     -d '{\"approved\": false, \"supervisor_id\": \"SUP-OPS-1\", \"note\": \"Declined by supervisor\"}'
   ```

---

## 4. DNS Mapping & CNAME Verification

Verify `employees.motahai.com` points to Wesam.ai:
```powershell
Resolve-DnsName employees.motahai.com -Type CNAME
```

Test HTTP and SSL certificate:
```powershell
curl.exe -I -k https://employees.motahai.com
```

---

## 5. Hermes Connectivity Check

Verify communication with Hermes Agent on Contabo VPS (`<VPS_IP>`):
```powershell
curl.exe -I -k https://hermes.motahai.com/health
```
