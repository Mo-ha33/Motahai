# Ameen Digital AI Workforce — DNS & Custom Domain Configuration
**Target Domain:** `https://employees.motahai.com`  
**Brand:** Ameen Digital AI Employees (`agency.motahai.com`)  
**Builder / Orchestrator:** Wesam.ai Platform  

---

## 1. Current DNS Status & Requirement

### Existing State:
```text
employees.motahai.com   A   <VPS_IP> (Points to Contabo VPS running the co-hosted app)
```

### Required State:
```text
employees.motahai.com   CNAME   cname.wesam.ai (or your designated Wesam Platform DNS target)
```

---

## 2. DNS Provider Configuration Guide

### Option A: Cloudflare DNS (Recommended)
1. Log in to the Cloudflare Dashboard for `motahai.com`.
2. Go to **DNS** -> **Records**.
3. Locate the existing record for `employees`:
   - If it is an **A Record** pointing to `<VPS_IP>`, click **Edit** and delete it or change it to CNAME.
4. Add New Record:
   - **Type:** `CNAME`
   - **Name:** `employees`
   - **Target:** `cname.wesam.ai` (or your assigned Wesam cluster hostname)
   - **Proxy status:** 
     - *DNS only (Grey Cloud)* during initial SSL provisioning on Wesam.
     - *Proxied (Orange Cloud)* once Wesam custom domain validation completes.
   - **TTL:** Auto

---

### Option B: GoDaddy / Namecheap / Hostinger
1. Navigate to DNS Management for `motahai.com`.
2. Delete the record: `A` -> `employees` -> `<VPS_IP>`.
3. Add record:
   - **Type:** `CNAME`
   - **Host:** `employees`
   - **Points to:** `cname.wesam.ai`
   - **TTL:** 300 seconds (5 minutes) or 1/2 hour.

---

## 3. Wesam.ai Platform Verification
1. In the **Wesam.ai Dashboard**, navigate to **Settings** -> **Custom Domain** (or **Workforce Portal**).
2. Enter `employees.motahai.com`.
3. Click **Verify CNAME & Provision SSL**.
4. Wesam automatically validates the DNS challenge and binds the custom domain to the Ameen Digital AI Employee swarm.

---

## 4. Verification CLI Commands

Run from Windows PowerShell:
```powershell
# Query CNAME record
Resolve-DnsName employees.motahai.com -Type CNAME

# Test HTTPS connection and SSL certificate
curl.exe -I -k https://employees.motahai.com
```
