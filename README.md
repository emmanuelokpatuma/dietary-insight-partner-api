# Dietary Insight Partner API (`/v1`)

Production-ready partner API for multi-business food apps (for example McDonald's, Sainsbury's, grocery and restaurant platforms).

This service is designed for embedded use inside partner ecosystems:
- Partner app calls partner backend.
- Partner backend calls this API server-to-server.
- Partner backend returns dietary suggestions to app UI before checkout.

API keys are never shipped in mobile/web clients.

## What This API Does Today

| Endpoint | Scope | Purpose |
|---|---|---|
| `POST /v1/menu-items/analyze` | `nutrition:read` | Analyze one menu item, with content-based caching. |
| `POST /v1/photos/analyze` | `photos:analyze` | Analyze food photo without storing the image. |
| `GET /v1/usage?from=YYYY-MM-DD&to=YYYY-MM-DD` | `usage:read` | Partner usage summary by date range. |
| `GET /healthz` | none | Health check for Cloud Run. |

## Integration Contract For Partner Apps

Use this flow for every partner business.

1. User adds food items to basket in partner app.
2. Partner app sends basket + dietary preferences to partner backend.
3. Partner backend calls this API (`/v1/menu-items/analyze` and optional `/v1/photos/analyze`).
4. Partner backend applies policy/rules to produce warnings, blocks, or swaps.
5. Partner backend sends final recommendation payload back to app before checkout.

### Suggested Partner Backend Request Shape

This is a partner-backend contract example, not a new endpoint in this API.

```json
{
  "user_requirements": ["nut_free", "low_sugar", "vegetarian"],
  "basket": [
    {
      "partner_item_id": "burger-001",
      "name": "Chicken burger",
      "official_nutrition": {
        "kcal": 640,
        "protein_g": 38,
        "carbs_g": 52,
        "fat_g": 29
      },
      "declared_allergens": ["gluten", "egg", "milk"]
    }
  ]
}
```

### Suggested Partner Backend Response Shape

```json
{
  "status": "ok",
  "flags": [
    {
      "partner_item_id": "burger-001",
      "severity": "warning",
      "reason": "Contains egg and milk",
      "matched_requirements": ["nut_free"]
    }
  ],
  "suggestions": [
    {
      "type": "swap",
      "from_partner_item_id": "burger-001",
      "to_partner_item_id": "burger-004",
      "message": "Try grilled chicken salad for lower fat"
    }
  ]
}
```

## API Design Standard (Sweet, Clean, Scalable)

Use these principles for a high-quality partner-facing API.

1. Multi-tenant by default.
- Every request is scoped to one partner key.
- Never leak cache, usage, or metadata across partners.

2. Server-to-server trust boundary.
- No client-embedded keys.
- Partner key validation, scope checks, revocation support, and key rotation.

3. Predictable contracts.
- Strict input schemas (`extra=forbid`).
- Stable versioning under `/v1`.
- Uniform error body: `{ "error": { "code", "message" } }`.
- Correlation with `X-Request-Id` on every response.

4. Cost and latency control.
- Cache by normalized input hash.
- Per-partner rate limits.
- Global AI daily cap and optional per-partner cap.
- Return cached results even when daily cap is reached.

5. Observability and operability.
- One structured log line per request.
- No sensitive payload/image logging.
- Health endpoint and SLO tracking.

6. Compliance and safety posture.
- Data minimization (photo analyzed in memory, then discarded).
- Allergen notice always returned.
- Business legal allergen info remains source of truth.

## GCP Architecture For Millions Of Users

Recommended production topology:

1. `Cloud Run` for stateless autoscaling API in `europe-west2`.
2. `HTTP(S) Load Balancer` or `API Gateway` in front of Cloud Run.
3. `Cloud Armor` for abuse and DDoS protection.
4. `Secret Manager` for `MONGO_URI` and `GEMINI_API_KEY`.
5. `MongoDB` for durable records and history.
6. `Memorystore (Redis)` for high-volume counters and rate-limiting hot path.
7. `Pub/Sub` for async usage aggregation at very high throughput.
8. `Cloud Monitoring + Logging` for p95 latency, 4xx/5xx, saturation, quota, and spend alerts.

### Scale Strategy

At pilot stage:
- MongoDB counters are acceptable.
- Keep Cloud Run max instances conservative.

At growth stage:
- Increase `MAX_INSTANCES`.
- Move rate limits and usage counters to Redis.
- Batch usage flushes to MongoDB.
- Track partner-level and global AI quota burn.

At enterprise stage:
- Define per-partner SLA and quota tiers.
- Add fail-open/fail-safe policy per route.
- Add secondary region strategy if required by contracts.

## Local Development

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
python -m pytest
```

Run locally:

```bash
cp .env.example .env
set -a; source .env; set +a
uvicorn partner_api.server:app --reload --port 8080
```

Docs (non-production only):
- `http://localhost:8080/v1/docs`

## Deploy To GCP (Cloud Run)

```bash
gcloud auth login
PROJECT_ID=your-project-id ./deploy/deploy.sh
```

The deploy script:
- Enables required APIs.
- Creates least-privilege service account.
- Stores secrets in Secret Manager.
- Deploys Cloud Run service with autoscaling settings.

## Onboard A New Partner

```bash
export MONGO_URI=...
python -m scripts.manage_partners create-partner --name "Partner Name" --plan pilot --contact ops@partner.example
python -m scripts.manage_partners create-key --partner-id ptn_... --mode test --scopes nutrition:read photos:analyze usage:read
```

Promotion flow:
1. Start with `test` key.
2. Validate integration and quality checks.
3. Sign legal and privacy docs.
4. Issue `live` key.
5. Revoke/rotate old keys regularly.

## Example Calls

```bash
URL=https://your-service-url
KEY=dik_test_...

curl -s $URL/v1/menu-items/analyze \
  -H "Authorization: Bearer $KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "partner_item_id": "burger-001",
    "name": "Chicken burger",
    "ingredients": ["chicken breast", "brioche bun", "lettuce", "mayonnaise"],
    "official_nutrition": {"kcal": 640, "protein_g": 38, "carbs_g": 52, "fat_g": 29},
    "declared_allergens": ["gluten", "egg", "milk"]
  }'

curl -s $URL/v1/photos/analyze \
  -H "Authorization: Bearer $KEY" \
  -F image=@meal.jpg \
  -F hint="lunch"

curl -s "$URL/v1/usage?from=2026-10-01&to=2026-10-31" \
  -H "Authorization: Bearer $KEY"
```

## Recommended Dietary Policy Format

Keep policy logic in partner backend for app-specific behavior.

```json
{
  "version": "2026-09",
  "rules": [
    {
      "id": "avoid_nuts",
      "if": {"allergens_any": ["nuts"]},
      "action": "block",
      "message": "Not suitable for nut allergy"
    },
    {
      "id": "high_calorie_warning",
      "if": {"nutrition": {"kcal_gt": 700}},
      "action": "warn",
      "message": "High calorie item"
    }
  ]
}
```

Allowed action examples:
- `block`
- `warn`
- `prefer`

## What To Do Next

1. Finalize partner integration contract.
- Publish one shared JSON contract for basket input and suggestion output.
- Keep it versioned (`contract_version`).

2. Production hardening.
- Add Redis for rate-limit and usage counters before major launch.
- Define SLOs and alert thresholds.
- Add partner quota and spend dashboards.

3. Partner rollout plan.
- Onboard 2-3 pilot partners first.
- Run accuracy review on top 50 items per partner.
- Collect rejection/override analytics from partner apps.

4. Security and compliance.
- Enforce key rotation policy.
- Complete DPIA and DPA workflow per partner.
- Pen test before broad rollout.

5. Product quality loop.
- Track user action after suggestion (accept, ignore, swap).
- Tune policy quality by business vertical (fast food vs grocery).
- Evolve to partner-specific recommendation profiles.

## Notes

- This API intentionally does not implement checkout UI behavior. That belongs in each partner app/backend.
- This API provides robust nutrition and allergen insights that partner apps can use at checkout time.
