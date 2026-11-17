# Outreach Templates v1（2026-11-13 起草，待用户审阅）

**发送纪律（不可违反）**：
- G1 达标前（hello@ 预计 11-30）不发任何一封
- From=hello@paycensus.com（显示名 PayCensus），署名 Harold Tucker（身份红线：不虚构头衔）
- 全部用 role 邮箱（企业公开商务地址，GDPR Recital 14），CAN-SPAM 三件套内建
- 每封个性化素材=该卖家自己的真实链上数据（"交付数据"非推销）
- 回复直达 hello@ 收件箱，24h 内 AI 升级回复红线照旧（不承诺收录效果）
- 退订：一行回 "stop" 即加 suppression list

**合并字段**（发送器从 census.db 注入）：
`{{domain}}` `{{addr}}` `{{vol_usd}}` `{{txs}}` `{{first_day}}` `{{seller_page}}`（=paycensus.com/s/{{addr}}.html）

---

## Template A — T3 机构提案信（10 家）

**Subject**: Machine-payment data: {{domain}} cited in a Bitcoin-anchored census

```
Hi {{domain}} team,

I run PayCensus (paycensus.com) — a neutral, Bitcoin-anchored census of the
machine-payment economy (x402 / MPP settlements, counted on-chain, $0 estimates
merged anywhere).

Your address appears in our registry as an active settlement counterparty:
  {{addr}} — {{txs}} settlements, {{vol_usd}} USDC since {{first_day}}
  public record: {{seller_page}}

Three things, no obligations:

1. FREE Verified listing. Domain-control verification (DNS TXT, deterministic,
   re-checked every 72h) is free forever. Agents and analysts increasingly
   query "who is behind this address" — a Verified record answers that.
   Start: https://trust.paycensus.com/verify.html

2. Citation rights. If you publish research or dashboards, our daily files
   (stats.json / calendar.json / indices.json) are open and hash-chained —
   cite any number, we will link your work back.

3. Corrections window. Anything wrong in your record, reply here and it is
   fixed within one business day. We publish corrections, never silently edit.

We never publish contact information — this email is the only use of the
address published on your own storefront.

Harold Tucker
PayCensus Labs — paycensus.com
The census of the machine economy

---
You receive this one-time note because {{domain}} is publicly listed as an
x402 seller. Reply "stop" to opt out permanently.
PayCensus Labs · paycensus.com
```

---

## Template B — T2 卖家转化信（167 家，分批 20/天）

**Subject**: Agents paid {{domain}} {{vol_usd}} USDC — but they see an anonymous address

```
Hi {{domain}} team,

Quick data point from the machine-payment census (paycensus.com, counted
on-chain, Bitcoin-anchored daily):

  Your settlement address {{addr}}
  — {{txs}} machine payments received
  — {{vol_usd}} USDC total, since {{first_day}}
  — live record: {{seller_page}}

What most x402 sellers do not know: right now, every AI agent and every
analytics tool that looks up that address sees an anonymous hex string.
You built the service; the identity layer is missing.

Fix takes 10 minutes and is free:
  1. Add one DNS TXT record (we generate it for you)
  2. Verify at https://trust.paycensus.com/verify.html
  3. Your record flips to "Verified" — checked automatically every 72 hours

After verifying, the Paid badge ($99/year, founding rate, first 100 sellers)
adds the seal + priority placement in the directory agents actually read.

No spam, no resale of your data: contact information is never published by
PayCensus — this email is the only use of the address on your own storefront.

Harold Tucker
PayCensus Labs — paycensus.com

---
Reply "stop" to opt out permanently.
PayCensus Labs · paycensus.com
```

---

## Template C — T1 开发者信（29 家）

**Subject**: pip install paycensus — the machine-payment census, now importable

```
Hi {{domain}} team,

Your address is already in the machine-payment census: {{txs}} settlements —
{{vol_usd}} USDC received — counted on-chain, Bitcoin-anchored.
Public record:
{{seller_page}}

For developers we ship a stdlib-only client:

  pip install paycensus

  from paycensus import PayCensus
  pc = PayCensus()
  pc.stats()        # daily settlement counts + volume
  pc.seller(addr)   # who paid whom, edges, daily curve
  pc.trust_status(addr)  # deterministic domain-control verification

Free tier: 10 API calls/day, no domain required, key in 2 minutes —
https://trust.paycensus.com/api.html

If your service accepts machine payments, claim your record (free, DNS-based,
self-serve): https://trust.paycensus.com/verify.html

Found a bug or want an endpoint? Reply here — I read everything.

Harold Tucker
PayCensus Labs — paycensus.com
github.com/OneLight1688/paycensus

---
Reply "stop" to opt out permanently.
PayCensus Labs · paycensus.com
```

---

## 发送计划（G1 达标后）

| 周次 | 动作 | 量 |
|---|---|---|
| W0 (11-30) | hello@ 达标确认（mail-tester 复测 ≥8.5 + 种子箱抽检） | 复测 |
| W0+1 | Template A：T3 10 家一次性发（单身份小量安全） | 10 |
| W0+1 起 | Template B：20/天爬坡，167 家 ~9 个工作日 | 167 |
| W0+2 起 | Template C：5/天穿插 | 29 |
| 回复处理 | AI 升级回复（24h 红线），hit→Stripe/verify 自助链 | — |

**DKIM 修复（发信前置，今天就做）**：mail-tester 实测 6.5/10，扣分主因=「邮件未经 DKIM 签名」(-1)。DNS 记录已在（default._domainkey），需在阿里邮箱管理后台开启 DKIM 签名开关（域管理 → 安全/反垃圾 设置）。修好后复测预期 ≥7.5；剩余扣分主要是域名年龄（FROM_FMBLA_NEWDOM14，注册 7-14 天新域规则，随时间自动消失）。
