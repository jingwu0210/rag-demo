# Technical Specifications

> Version: 2026.0, Platform Engineering
> This document defines the technical standards, conventions and architecture
> principles that apply to all services and deliverables in the organisation.

## 1. Introduction

1.1 This specification defines how services are built, deployed, operated and documented.

1.2 All new services must conform to these standards unless an approved exception is granted.

1.3 Exceptions are recorded in the architecture decision log (ADR) with rationale.

1.4 The standards apply to internal tools, customer-facing services and data pipelines.

1.5 Platform Engineering maintains this document and reviews it quarterly.

1.6 Feedback and proposed changes are submitted via a pull request to this repository.

1.7 Compliance with these standards is verified in the release pipeline.

1.8 Terminology follows the glossary published in the architecture repository.

1.9 The most recent version of this document is the authoritative source.

1.10 Ambiguities are resolved by opening a question in the platform channel.

## 2. API Conventions

2.1 All REST endpoints use JSON and must be idempotent where feasible.

2.2 Request IDs are passed in the X-Request-Id header.

2.3 The X-Request-Id is echoed in the response and in all log lines.

2.4 Endpoints use nouns, not verbs, in their resource paths.

2.5 Resources are nested only when the relationship is permanent.

2.6 Collection paths are plural (e.g. /customers, /orders).

2.7 Field names use lowerCamelCase in JSON.

2.8 Timestamps use ISO 8601 with a UTC offset (e.g. 2026-08-11T10:00:00Z).

2.9 Money values are represented as integer minor units with a currency field.

2.10 Null fields are omitted from responses unless explicitly required by contract.

2.11 Responses are always wrapped in a stable envelope where required by the API gateway.

2.12 Endpoints must document their request and response schemas.

2.13 Breaking changes require a new API version (see Section 8).

2.14 Deprecated fields are removed only after a published deprecation window.

2.15 Services expose a GET /healthz endpoint returning HTTP 200 when ready.

2.16 Liveness and readiness may be separate endpoints where deployment requires it.

2.17 All endpoints are TLS-only in every environment except local development.

## 3. HTTP Status and Errors

3.1 Status codes follow the semantics of RFC 9110.

3.2 Successful reads return 200; successful creations return 201 with a Location header.

3.3 No-content success returns 204 with an empty body.

3.4 Client errors return 4xx; validation failures return 422.

3.5 Authentication failures return 401; authorization failures return 403.

3.6 Missing resources return 404 with a stable error code.

3.7 Conflicting state returns 409 with the conflicting resource identifier.

3.8 Server errors return 5xx and are automatically alerted.

3.9 Error responses follow the shape {"error": {"code": "...", "message": "..."}}.

3.10 Error codes are stable, documented and never reused for different meanings.

3.11 Error messages are human-readable and localisable.

3.12 Technical details are omitted from public error messages.

3.13 Services should never leak stack traces to clients.

3.14 Correlation between errors and logs uses the X-Request-Id.

## 4. Authentication and Authorization

4.1 Authentication is performed by the central identity provider.

4.2 Machine-to-machine calls use OAuth 2.0 client credentials.

4.3 User-facing services use OpenID Connect.

4.4 Long-lived API keys are prohibited; short-lived tokens are rotated automatically.

4.5 Secrets are injected via environment variables, never committed to source control.

4.6 Authorization is enforced per-route and per-resource, not only at the gateway.

4.7 Multi-tenancy is enforced by tenant scoping in every query.

4.8 System accounts are provisioned through the identity platform, not ad hoc.

4.9 Revoked identities propagate within five minutes.

4.10 Audit logs record who did what, when and to which resource.

4.11 Privileged actions require a second-factor approval where supported.

4.12 We follow the principle of least privilege.

## 5. Request and Response Schemas

5.1 Schemas are defined in code and published to a schema registry.

5.2 Schema changes follow semver-style compatibility rules.

5.3 Unknown fields in requests are rejected to prevent silent drift.

5.4 Default values are documented explicitly.

5.5 Enumerations are validated against a closed set unless stated otherwise.

5.6 File uploads use multipart/form-data and enforce size limits.

5.7 Streaming responses use chunked transfer encoding.

5.8 Response caching is controlled with explicit Cache-Control headers.

5.9 Conditional requests (ETag/If-None-Match) are supported on large resources.

5.10 Schema documentation is generated from the code definitions.

## 6. Pagination, Filtering and Sorting

6.1 Collection endpoints support cursor-based pagination.

6.2 Cursor pagination returns a next_cursor field and a page_size limit.

6.3 Page size is capped at a documented maximum (default 100).

6.4 Offset pagination is allowed only for small, stable collections.

6.5 Filters use query parameters with documented operators.

6.6 Sorting uses a sort parameter with field and direction (e.g. sort=-created_at).

6.7 The default sort order is documented per endpoint.

6.8 Total counts are returned only when cheap to compute.

6.9 Pagination results are stable across concurrent writes where feasible.

6.10 Filtering on nested fields is documented explicitly.

## 7. Rate Limiting

7.1 Public endpoints are rate-limited per client.

7.2 Rate-limit headers follow the standard draft convention.

7.3 Exceeded limits return 429 with a Retry-After header.

7.4 Internal services enforce generous but non-infinite limits.

7.5 Rate limiting protects both the service and its dependencies.

7.6 Burst allowances are documented per tier.

7.7 Rate-limit violations are logged with the client identifier.

7.8 Throttling is configured via the gateway, not inside business code.

## 8. Versioning

8.1 API versions are encoded in the URL path (e.g. /v1, /v2).

8.2 A version is released only when fully backward-incompatible changes are required.

8.3 Additive changes do not require a version bump.

8.4 Deprecated versions are supported for a minimum of twelve months.

8.5 Version support windows are published in the release notes.

8.6 Clients pin the version they integrate against.

8.7 The current version is always /v1 unless announced otherwise.

## 9. Idempotency

9.1 Mutating endpoints accept an Idempotency-Key header.

9.2 Replayed requests with the same key return the original response.

9.3 Idempotency keys are retained for at least twenty-four hours.

9.4 The server responds 422 if a key is reused with a different payload.

9.5 Idempotency keys must be unique per client per resource.

9.6 We guarantee at-least-once processing and at-most-once effects.

9.7 Consumers must handle duplicate delivery gracefully.

## 10. Asynchronous Jobs

10.1 Long-running work is queued rather than performed inline in a request.

10.2 Job endpoints return a job id immediately.

10.3 Job status is pollable via GET /jobs/{id}.

10.4 Jobs report progress and a terminal status (succeeded/failed/cancelled).

10.5 Failed jobs expose a stable error code and a retry policy.

10.6 Job queues are durable and survive process restarts.

10.7 Dead-letter queues capture permanently failing messages.

10.8 Alerts fire when dead-letter queues grow beyond a threshold.

10.9 Job execution is idempotent so retries are safe.

## 11. Webhooks

11.1 Outbound events are delivered via signed webhooks.

11.2 Payloads are signed with HMAC-SHA256 and include a timestamp.

11.3 Delivery is retried with exponential backoff up to a documented maximum.

11.4 Consumers acknowledge delivery with HTTP 2xx.

11.5 Event ordering is best-effort and documented per event type.

11.6 A replay endpoint allows consumers to backfill missed events.

11.7 Webhook URLs are validated and secrets are stored encrypted.

## 12. Deployment and Environments

12.1 Services run on Linux x86_64.

12.2 Environments are named dev, staging and production.

12.3 Deployments are automated and repeatable from a single pipeline.

12.4 Infrastructure is defined as code and reviewed like application code.

12.5 Releases are immutable; configuration changes do not trigger re-deploys.

12.6 Health checks use GET /healthz and must return HTTP 200 when the service is ready.

12.7 Rollbacks are executed by deploying the previous immutable release.

12.8 Feature flags control gradual rollouts and are always reversible.

12.9 Environment parity is enforced with the same image in all environments.

12.10 Production access requires elevated, audited credentials.

12.11 Schedules and runbooks are documented for every service.

12.12 Deployment windows are agreed with the on-call rotation.

## 13. Configuration Management

13.1 Configuration is separated from code and versioned.

13.2 Environment-specific values live in environment-specific configuration.

13.3 Secrets are managed by the secrets vault, never in plain files.

13.4 Configuration changes are auditable and attributable.

13.5 Services validate their configuration at startup and fail fast.

13.6 Defaults are safe for development and overridden in production.

13.7 Feature toggles are documented with owner and expiration date.

13.8 Rotation of secrets is supported without code changes.

## 14. Observability

14.1 Every service emits structured logs in JSON.

14.2 Log lines carry the X-Request-Id, service, and event name.

14.3 Logs do not contain secrets or personal data.

14.4 Log levels are used consistently: debug, info, warn, error.

14.5 Errors are logged with the error type and a stable message.

14.6 Services expose Prometheus-format metrics on a dedicated port.

14.7 RED metrics (rate, errors, duration) are mandatory for every endpoint.

14.8 Distributed tracing propagates trace context across services.

14.9 Trace sampling preserves high-cardinality debugging for errors.

14.10 Dashboards exist for latency, error rate, saturation and traffic.

14.11 Alerts are actionable, owned and tested.

14.12 On-call documentation is linked from the alert.

14.13 Dependency health is tracked alongside service health.

14.14 Log retention and metric retention follow the retention policy.

14.15 New services ship with an observability checklist before go-live.

## 15. Security Standards

15.1 Every service undergoes a security review before production.

15.2 Dependency scanning runs on every merge.

15.3 Static analysis is wired into the build.

15.4 Secrets are never logged, even masked.

15.5 Input validation happens at the boundary.

15.6 Output encoding prevents injection of any kind.

15.7 We follow the OWASP Top Ten as a baseline.

15.8 Security patches are applied within the agreed SLA.

15.9 End-of-life dependencies are upgraded proactively.

15.10 Access to production data is minimised and audited.

15.11 Encryption in transit is mandatory; at rest is default.

15.12 Third-party code is scanned before adoption.

15.13 Security incidents follow the published incident response runbook.

15.14 The security team is notified of any suspected compromise immediately.

## 16. Database Conventions

16.1 Database access is centralised through a repository layer.

16.2 Schema migrations are versioned and applied in order.

16.3 Migrations are reversible or tested for rollback.

16.4 Destructive operations on production data require written approval.

16.5 Indexes are reviewed for every new query pattern.

16.6 We avoid N+1 query patterns in application code.

16.7 Long transactions are broken into shorter units of work.

16.8 Soft deletes are preferred where auditability matters.

16.9 Read replicas are used for reporting workloads where appropriate.

16.10 Backups are tested regularly and restore times are measured.

16.11 Sensitive columns are encrypted at rest or tokenised.

16.12 Every table has created_at and updated_at columns unless documented otherwise.

## 17. Messaging and Eventing

17.1 Event schemas are versioned and evolve additively.

17.2 Events are emitted after the local transaction commits.

17.3 Consumers are idempotent by event id.

17.4 Events carry enough context to be processed independently.

17.5 We prefer events over request-reply for cross-service coordination.

17.6 Message ordering is guaranteed only where explicitly documented.

17.7 Poison messages are quarantined, not silently dropped.

17.8 Topic naming follows the platform convention.

17.9 Dead-letter handling is monitored and tested.

## 18. Frontend Conventions

18.1 The web application is a single-page application served over HTTPS.

18.2 Component libraries are shared via the internal design system.

18.3 Accessibility follows WCAG 2.2 AA as a baseline.

18.4 Client-side validation mirrors server-side validation.

18.5 Frontend and backend share the schema definitions where feasible.

18.6 Error states are designed for every data-fetching component.

18.7 Page performance budgets are defined per route.

18.8 Telemetry is collected for user-facing errors.

18.9 Sensitive data is never rendered to the DOM unless required.

## 19. Testing and CI/CD

19.1 Unit tests run on every push.

19.2 Integration tests run in the merge pipeline.

19.3 Coverage gates are set per repository and published.

19.4 Test data is generated deterministically, not copied from production.

19.5 End-to-end smoke tests run before every release.

19.6 CI artifacts are immutable and signed.

19.7 The pipeline enforces linting, formatting and type-checking.

19.8 Changes are reviewed by at least one peer.

19.9 Failed pipelines block merging and release.

19.10 Performance regression tests guard critical endpoints.

19.11 Test reports are visible to the whole team.

19.12 Contract tests protect provider-consumer compatibility.

## 20. Architecture and Documentation Standards

20.1 Design documents are stored as Markdown under the docs/ directory.

20.2 Section headers are preserved so that retrieval can anchor answers to the exact section.

20.3 ADRs record significant architectural decisions with context and consequences.

20.4 Diagrams are maintained as code where feasible.

20.5 Documentation is reviewed when the related code changes.

20.6 A README explains how to run, test and operate each service.

20.7 Runbooks describe recovery procedures for common incidents.

20.8 On-call engineers can reach documentation from the alert.

20.9 Architecture reviews are held for designs that cross service boundaries.

20.10 Documentation rot is treated as a defect and fixed like a bug.

## 21. AI and Automation Standards

21.1 AI-assisted features must be reviewed for accuracy, bias and safety before release.

21.2 Model outputs are treated as untrusted until validated.

21.3 Retrieval-augmented generation must cite the source of each claim.

21.4 Answers must be grounded in the retrieved context and refuse to answer otherwise.

21.5 Model versions are pinned and upgraded through the release process.

21.6 Prompts and system instructions are versioned like code.

21.7 Prompt-injection attempts are detected and handled safely.

21.8 Personal data must not be sent to external model providers without approval.

21.9 Redaction of personal data is applied before data leaves the boundary.

21.10 Model latency and cost are measured per request and monitored.

21.11 Degraded model availability must not block critical business paths.

21.12 Automated decisions that affect people are reviewed for fairness.

21.13 Human oversight is retained for high-impact automated decisions.

21.14 Training data provenance is documented for every model in use.

21.15 Evaluation datasets are versioned and shared across teams.

21.16 Benchmarks are run before and after any model or prompt change.

21.17 Drift in model quality triggers a review of the deployment.

21.18 Automation must fail safely and gracefully when inputs are unusual.

21.19 Rollback of a model or prompt is always possible and documented.

21.20 Security reviews cover the full automation pipeline, not just the model.

21.21 Automation outcomes are logged with enough context to diagnose issues.

21.22 We do not automate processes that require regulatory judgement without oversight.

21.23 Third-party AI providers are assessed like any other critical vendor.

21.24 AI usage policies are communicated to all employees annually.

## 22. API Design Deep Dive

22.1 Resource paths use plural nouns and stable identifiers.

22.2 Actions that are not CRUD are modelled as sub-resources.

22.3 URL parameters are validated against documented patterns.

22.4 Query parameters that change semantics require explicit names.

22.5 We prefer nested resources only where the hierarchy is permanent.

22.6 API responses include a self link and related resource links where useful.

22.7 We document every endpoint's request, response and error contract.

22.8 Backward-compatible fields are added without version bumps.

22.9 We do not repurpose a field's meaning within a version.

22.10 Deprecation adds a Sunset header and a migration guide.

22.11 Batch operations are bounded by a documented maximum size.

22.12 API pagination defaults are consistent across the platform.

22.13 We validate content types and reject unsupported ones.

22.14 Endpoints return precise, actionable error responses.

22.15 We maintain an API style guide that all teams follow.

## 23. Error Handling Procedures

23.1 Every service defines a stable error taxonomy.

23.2 Errors carry a machine-readable code and a human message.

23.3 Validation errors include per-field messages and paths.

23.4 We do not expose internal identifiers in error responses.

23.5 Idempotency conflicts return a specific, documented code.

23.6 Downstream failures are mapped to appropriate client-facing errors.

23.7 Timeouts surface as a documented gateway error.

23.8 We distinguish retryable from non-retryable errors.

23.9 Retryable errors advertise a Retry-After where applicable.

23.10 Error handling is tested across the boundary.

23.11 We log the error context without logging secrets.

23.12 Client error responses are consistent across services.

23.13 Monitoring alerts on elevated error rates per endpoint.

23.14 Error rates are part of the release readiness criteria.

## 24. Authentication Patterns

24.1 Interactive users authenticate via the SSO portal.

24.2 Service-to-service calls use short-lived client-credential tokens.

24.3 Tokens are transmitted only over TLS.

24.4 We do not store raw tokens in application logs.

24.5 Token expiry is enforced on the resource server.

24.6 Refresh tokens are rotated on use.

24.7 Client secrets are stored in the vault, never in code.

24.8 Authentication failures are rate-limited to prevent brute force.

24.9 We support standard OAuth scopes for least privilege.

24.10 New identity integrations are reviewed by the security team.

24.11 Session length follows the platform session policy.

24.12 Multi-factor authentication is required for privileged accounts.

24.13 Account lockout and recovery follow the identity playbook.

24.14 We revoke access promptly on offboarding or role change.

## 25. Data Modeling Conventions

25.1 Entities are named in the singular by default.

25.2 Foreign keys are indexed where they drive joins.

25.3 We use consistent naming for timestamps and status fields.

25.4 Enumerated statuses are stored as stable string codes.

25.5 Money is stored as integer minor units with a currency column.

25.6 We avoid storing derived data that can be computed.

25.7 Soft deletes are used where auditability is required.

25.8 Audit trails record who changed what and when.

25.9 We document the data ownership for every entity.

25.10 Schema evolution is additive and backward-compatible.

25.11 Sensitive fields are identified and protected at the model level.

25.12 We prefer explicit joins over ORM magic where clarity matters.

25.13 Indexes are created based on measured query patterns.

25.14 Data residency requirements are applied at the model level.

## 26. Caching Standards

26.1 Caching is applied where reads dominate and freshness allows.

26.2 Cache keys are deterministic and versioned.

26.3 We define invalidation rules for every cache entry.

26.4 Cache-aside is the default pattern unless another is justified.

26.5 We do not cache per-user data in shared caches without care.

26.6 Cache hits and misses are observable via metrics.

26.7 Stale-while-revalidate is used where latency matters.

26.8 We set explicit TTLs for all cached content.

26.9 Cache stampedes are prevented with request coalescing.

26.10 Cache invalidation on write is tested.

26.11 We avoid caching requests that carry sensitive data.

26.12 Cache size and eviction are monitored.

26.13 Warm-up procedures exist for critical cached data.

26.14 We document caching behaviour in the service README.

## 27. Performance Budgets

27.1 Every endpoint has a defined latency budget.

27.2 We measure p50, p95 and p99 latency per endpoint.

27.3 Latency budgets are part of the service contract.

27.4 Page and API budgets are published in the performance registry.

27.5 We do not deploy regressions beyond the agreed budget.

27.6 Performance tests run in the merge pipeline.

27.7 Load tests simulate realistic concurrency and data volume.

27.8 We profile hot paths and optimise measured bottlenecks.

27.9 Database query latency is tracked per endpoint.

27.10 Frontend bundle size is capped per route.

27.11 We measure time-to-first-byte and first-contentful-paint.

27.12 Performance regressions are investigated before release.

27.13 Capacity planning uses measured utilisation trends.

27.14 Budgets are revisited as the workload evolves.

## 28. Reliability Engineering

28.1 Services define explicit reliability objectives (SLOs).

28.2 Error budgets govern how aggressively we can ship.

28.3 We track reliability against SLOs continuously.

28.4 Burn-rate alerts fire before the error budget is exhausted.

28.5 Redundancy is designed for availability-critical components.

28.6 We test failure modes through chaos experiments where safe.

28.7 Graceful degradation is preferred over hard failure.

28.8 Retries use exponential backoff with jitter.

28.9 Circuit breakers protect dependent services.

28.10 Bulkheads isolate failure domains.

28.11 Timeouts are explicit on every outbound call.

28.12 We cap in-flight requests to bound queue growth.

28.13 Capacity limits trigger backpressure rather than crashes.

28.14 Post-incident reviews identify and track mitigations.

## 29. On-Call and Incident Response

29.1 Every production service has a named on-call rotation.

29.2 On-call engineers are trained before taking rotation.

29.3 Incidents are declared through the standard channel.

29.4 A single incident commander coordinates the response.

29.5 We post status updates at regular intervals.

29.6 Incident timelines are recorded as they happen.

29.7 We communicate externally through the designated channel.

29.8 Severity definitions are documented and understood.

29.9 Escalation paths are tested periodically.

29.10 Runbooks guide recovery for known incident types.

29.11 We hold a post-incident review for every major incident.

29.12 Action items from reviews are tracked to closure.

29.13 On-call load is balanced and reviewed.

29.14 Downtime is measured against the availability SLO.

## 30. API Gateway and Routing

30.1 All public traffic passes through the API gateway.

30.2 The gateway terminates TLS and manages certificates.

30.3 Routing rules map paths to services in the registry.

30.4 The gateway enforces authentication at the edge.

30.5 Rate limiting is applied at the gateway.

30.6 We centralise request logging at the gateway.

30.7 The gateway propagates the X-Request-Id header.

30.8 Canary routing is supported via gateway rules.

30.9 We avoid business logic in the gateway.

30.10 Gateway configuration is versioned as code.

30.11 Blue-green switches are managed through the gateway.

30.12 The gateway strips sensitive headers before forwarding.

30.13 Gateway timeouts align with service latency budgets.

## 31. Contract Testing

31.1 Provider-consumer contracts are captured in code.

31.2 Contract tests run in both provider and consumer pipelines.

31.3 A contract change requires coordinated verification.

31.4 We version contracts alongside the API version.

31.5 Breaking contract changes are flagged in CI.

31.6 Contract files are reviewed like code.

31.7 We document the compatibility policy per contract.

31.8 Consumer expectations are published and discoverable.

31.9 Contract verification runs against the deployed provider.

31.10 We keep contract suites small and focused.

31.11 Contract test failures block merging.

31.12 Compatibility is re-verified after provider changes.

## 32. Observability Deep Dive

32.1 Logs, metrics and traces are correlated by request id.

32.2 Log lines are structured and machine-parseable.

32.3 We emit business events that support audit and analytics.

32.4 Metrics use consistent naming and label conventions.

32.5 Histograms capture latency distributions, not just averages.

32.6 We alert on symptom-based signals, not just causes.

32.7 Dashboards are owned and documented per service.

32.8 Trace sampling preserves full detail for errors.

32.9 We measure dependency health end to end.

32.10 Observability data has a defined retention period.

32.11 We export metrics in Prometheus format on a dedicated port.

32.12 Distributed tracing uses the platform's trace header.

32.13 New endpoints ship with the observability checklist.

32.14 Observability gaps found during incidents are tracked as defects.

## 33. Security Hardening

33.1 Services run with the least privilege required.

33.2 We disable unused services, ports and dependencies.

33.3 Container images are signed and scanned.

33.4 Base images are patched and rebuilt on a schedule.

33.5 We enforce secure defaults in frameworks.

33.6 CSRF, XSS and injection protections are verified per framework.

33.7 We set strict security headers on web responses.

33.8 CORS is restricted to documented origins.

33.9 We validate redirects to prevent open-redirect abuse.

33.10 File uploads are scanned and content-verified.

33.11 We rate-limit login and sensitive endpoints.

33.12 Secrets are rotated on schedule and after exposure.

33.13 We apply the principle of least surprise to error messages.

33.14 Security findings are remediated within the agreed SLA.

## 34. Compliance and Data Residency

34.1 Services identify the regulations that apply to their data.

34.2 Data residency is enforced per jurisdiction.

34.3 We document where data is stored and processed.

34.4 Cross-border data transfer follows approved mechanisms.

34.5 Audit logs are retained per compliance requirements.

34.6 We support data subject rights through API and process.

34.7 Records of processing activities are maintained.

34.8 Compliance requirements are encoded as tests where possible.

34.9 We notify on personal data breaches per policy.

34.10 Consent and lawful-basis records are kept accurate.

34.11 We respond to regulatory requests through the designated function.

34.12 Compliance controls are included in the security review.

## 35. Documentation Standards Deep Dive

35.1 Documentation is written for the reader, not the writer.

35.2 We use plain, active language and concrete examples.

35.3 API reference pages include request, response and error examples.

35.4 Getting-started guides take a user from zero to a working call.

35.5 We maintain a single source of truth and link rather than copy.

35.6 Documentation is versioned with the product.

35.7 We review docs for accuracy whenever code changes.

35.8 Technical terms are defined on first use.

35.9 We document deprecations and migration paths.

35.10 Runbooks are tested by someone other than the author.

35.11 We keep a changelog for every service.

35.12 Documentation linting runs in the pipeline.

35.13 We publish docs where engineers actually look for them.

35.14 Documentation rot is treated as a defect.

## 36. Frontend Performance

36.1 We measure real-user performance in production.

36.2 Core Web Vitals are tracked per route.

36.3 Lazy loading is used for below-the-fold content.

36.4 We code-split the application by route.

36.5 Images are sized, compressed and served responsively.

36.6 We avoid render-blocking resources where possible.

36.7 State management is kept predictable and minimal.

36.8 Virtualisation is used for long lists.

36.9 We debounce expensive input handlers.

36.10 Animations run on the compositor where possible.

36.11 We monitor JavaScript bundle size against budget.

36.12 Page weight budgets are enforced in CI.

36.13 Network waterfalls are reviewed for regressions.

36.14 Frontend performance is part of release review.

## 37. Mobile Conventions

37.1 Mobile apps follow the platform's human-interface guidelines.

37.2 We support the last two major OS versions.

37.3 Sensitive data is stored in the platform secure enclave.

37.4 We handle offline states gracefully.

37.5 Push notifications require clear user consent.

37.6 App telemetry is privacy-compliant.

37.7 We test on real devices in CI.

37.8 Release builds are signed and reproducible.

37.9 Deep links are documented and validated.

37.10 We handle background and foreground lifecycle correctly.

37.11 Accessibility is tested on mobile.

37.12 We plan for app-store review requirements.

37.13 Crash reporting is configured before release.

37.14 Feature rollout uses phased app-store release.

## 38. Infrastructure Standards

38.1 Compute is provisioned as code.

38.2 We use managed services where they reduce operational risk.

38.3 Environments are isolated by design.

38.4 Resources are tagged for cost and ownership.

38.5 We enforce least-privilege IAM policies.

38.6 Infrastructure changes go through the same review as code.

38.7 We plan capacity from measured utilisation.

38.8 Backup and restore are tested regularly.

38.9 Network segments isolate tiers of trust.

38.10 We use infrastructure modules that are reviewed centrally.

38.11 Deprovisioning is automated and verified.

38.12 Drift between declared and actual state is detected.

38.13 We follow the platform's golden-path templates.

38.14 Cost is reviewed monthly against budget.

## 39. Networking and Connectivity

39.1 Internal traffic uses the platform's service mesh.

39.2 We encrypt traffic in transit by default.

39.3 DNS records are managed as code.

39.4 Load balancers distribute traffic across healthy instances.

39.5 We define network policies between services.

39.6 Public endpoints are minimised.

39.7 We use a single egress path with allowlisting.

39.8 Latency between regions is considered in design.

39.9 Connection pools are sized and monitored.

39.10 We avoid long-lived client connections without reason.

39.11 Health checks determine routing eligibility.

39.12 Network incidents are covered by the runbook.

## 40. Secrets Management

40.1 Secrets are stored in the central vault.

40.2 We never commit secrets to source control.

40.3 Environment variables reference vault paths, not values.

40.4 Secret rotation is automated where supported.

40.5 Access to secrets is audited and least-privileged.

40.6 We avoid secrets in image layers.

40.7 Static analysis blocks accidental secret commits.

40.8 Secrets are not logged or echoed.

40.9 We use short-lived credentials for machine access.

40.10 Emergency access is recorded and reviewed.

40.11 Secret values are masked in dashboards.

40.12 We document which systems hold which secrets.

## 41. Logging Standards

41.1 Logs are structured JSON at every layer.

41.2 We log at the right level: debug, info, warn, error.

41.3 Log lines include service, environment and request id.

41.4 We log the outcome, not just the call.

41.5 Sensitive fields are redacted before logging.

41.6 Correlation ids tie logs to traces and metrics.

41.7 We avoid logging high-cardinality values.

41.8 Log volume is reviewed and right-sized.

41.9 We retain logs per the retention policy.

41.10 Error logs include the stable error code.

41.11 We log entry and exit for key business operations.

41.12 Logging failures do not break the request.

41.13 We search logs through the central platform.

41.14 Log conventions are part of code review.

## 42. Metrics Standards

42.1 Metrics use the RED framework: rate, errors, duration.

42.2 Every endpoint exposes request rate by status.

42.3 Error metrics are classified by error type.

42.4 Duration histograms capture latency distributions.

42.5 We expose saturation metrics for queues and pools.

42.6 Metric names follow the platform naming convention.

42.7 Labels are bounded to avoid high cardinality.

42.8 We document each metric's meaning and units.

42.9 Business metrics complement technical metrics.

42.10 Metrics are exported in Prometheus format.

42.11 Dashboards are linked from the service README.

42.12 We alert on both symptoms and known causes.

42.13 Metric retention follows the platform policy.

42.14 New metrics are added through a reviewable change.

## 43. Tracing Standards

43.1 Every service propagates the trace context.

43.2 Spans carry service, operation and status.

43.3 We set meaningful span names for readability.

43.4 Trace ids are propagated via the standard header.

43.5 We sample adaptively while preserving error traces.

43.6 Spans are annotated with key request attributes.

43.7 We avoid logging what traces already capture.

43.8 Cross-service causality is visible in the trace view.

43.9 Trace data is retained for diagnosis.

43.10 We instrument database and external calls.

43.11 Distributed traces cover async and queued work.

43.12 Trace quality is reviewed for gaps.

## 44. Database Deep Dive

44.1 We choose the storage engine for the access pattern.

44.2 Transaction boundaries are explicit and short.

44.3 We prefer eventual consistency where business logic allows.

44.4 Query plans are reviewed for new hot queries.

44.5 We batch where latency demands it.

44.6 Connection pooling is configured per workload.

44.7 We monitor slow queries and lock waits.

44.8 Schema changes are rolled out without long locks.

44.9 We avoid cross-database joins.

44.10 Data is sharded only when a clear key exists.

44.11 We test migrations against production-like data.

44.12 Backups are encrypted and tested.

44.13 Read replicas are used for reporting workloads.

44.14 We document the data lifecycle per store.

## 45. Message Queue Deep Dive

45.1 Queues are used for durable, decoupled work.

45.2 We prefer at-least-once delivery with idempotent consumers.

45.3 Message schemas are versioned and additive.

45.4 We set visibility and retention timeouts deliberately.

45.5 Poison messages are moved to a dead-letter queue.

45.6 Consumer lag is monitored and alerted.

45.7 We test queue behaviour under failure.

45.8 Message ordering is only relied on where documented.

45.9 We cap message size and payload complexity.

45.10 Producers publish after the local commit.

45.11 We document queue ownership and SLAs.

45.12 Consumers scale with the processing rate.

## 46. Cloud Service Usage

46.1 Managed services are preferred over self-hosting.

46.2 We use the platform's approved service catalogue.

46.3 Service tiers are sized from measured need.

46.4 We enable automated scaling where appropriate.

46.5 Cloud resource limits are set and monitored.

46.6 We review service configurations for security defaults.

46.7 Cloud costs are attributed to the owning team.

46.8 Multi-region is adopted only where required.

46.9 We follow the provider's resilience guidance.

46.10 Deprecated cloud features are migrated early.

46.11 Cloud access is scoped by role.

46.12 We review cloud usage for waste regularly.

## 47. Cost Optimization

47.1 We measure cost per request and per business outcome.

47.2 Idle resources are identified and deprovisioned.

47.3 Right-sizing is reviewed periodically.

47.4 We use spot or reserved capacity where stable.

47.5 Storage tiers match access frequency.

47.6 Data retention is aligned to value.

47.7 We avoid duplicated data across stores.

47.8 Cost anomalies are flagged and investigated.

47.9 We budget for growth and communicate it.

47.10 Cost data is visible to engineering teams.

47.11 Optimisation does not compromise SLOs.

47.12 We review cost before each major release.

## 48. AI Engineering Deep Dive

48.1 Models are evaluated on a versioned evaluation set.

48.2 We measure latency, cost and quality per model.

48.3 Prompts are versioned and reviewed like code.

48.4 We implement guardrails for harmful output.

48.5 Retrieval quality is measured with retrieval metrics.

48.6 We test for prompt-injection resistance.

48.7 Model outputs are grounded and cited where required.

48.8 We monitor for drift and degradation.

48.9 Human review is retained for high-stakes outputs.

48.10 We log model requests with traceability.

48.11 Personal data is minimised before model calls.

48.12 Third-party model providers meet vendor standards.

48.13 We document model provenance and training data.

48.14 Rollback of model changes is always possible.

## 49. Developer Experience

49.1 Local development mirrors production as closely as practical.

49.2 We provide a one-command local setup.

49.3 Services expose local seed data for development.

49.4 We maintain fast feedback loops in CI.

49.5 Developer documentation is kept current.

49.6 We use a consistent language and framework per platform.

49.7 Internal libraries are published to the internal registry.

49.8 We automate boilerplate with generators and templates.

49.9 Code review is fast and constructive.

49.10 We invest in tooling that removes toil.

49.11 Onboarding documentation is tested by newcomers.

49.12 We measure and improve developer satisfaction.

49.13 Environment variables are documented per service.

49.14 We celebrate good engineering craftsmanship.

## 50. Engineering Process

50.1 Work is tracked in the platform issue tracker.

50.2 User stories are small and independently shippable.

50.3 We plan in iterations with reviewable outcomes.

50.4 Code is reviewed by at least one peer.

50.5 We merge to trunk or short-lived branches.

50.6 Every change is tied to a ticket or PR.

50.7 Releases follow a documented cadence.

50.8 We hold retrospectives and act on findings.

50.9 Technical debt is tracked and prioritised.

50.10 We write tests before or with the code.

50.11 Definition of done includes docs and observability.

50.12 Cross-team changes are coordinated through the platform.

50.13 We follow the engineering principles document.

50.14 Process is adapted based on team learning.

## 51. API Security Patterns

51.1 We authenticate every request at the edge.

51.2 Authorization is enforced per resource.

51.3 We validate all input at the boundary.

51.4 Responses are encoded to prevent injection.

51.5 We protect against cross-site request forgery.

51.6 Rate limiting guards sensitive endpoints.

51.7 We do not expose internal topology in errors.

51.8 Pagination bounds prevent resource exhaustion.

51.9 We avoid returning more data than requested.

51.10 Security headers are set on all responses.

51.11 We scan dependencies continuously.

51.12 Security review is part of the pipeline.

51.13 We rotate keys and certificates on schedule.

51.14 Incident response follows the runbook.

## 52. Frontend Architecture

52.1 The frontend is modular and component-based.

52.2 State lives close to where it is used.

52.3 We separate concerns between view and logic.

52.4 Routing is declarative and typed.

52.5 We share types between frontend and backend.

52.6 Design tokens come from the design system.

52.7 We avoid framework lock-in where possible.

52.8 Accessibility is a first-class requirement.

52.9 We test components and user flows.

52.10 Performance budgets apply to the frontend too.

52.11 We handle loading and error states everywhere.

52.12 Telemetry covers user-facing failures.

52.13 We keep the frontend dependency surface small.

52.14 Frontend architecture is reviewed like backend.

## 53. Data Pipeline Standards

53.1 Pipelines are versioned, tested and repeatable.

53.2 We track data lineage for every dataset.

53.3 Data quality checks run at each stage.

53.4 We handle schema drift explicitly.

53.5 Pipelines are idempotent where rerun is possible.

53.6 We monitor pipeline latency and failures.

53.7 Sensitive data is redacted early in the pipeline.

53.8 We document dataset owners and SLAs.

53.9 Backfills are safe and reversible.

53.10 We test against representative data.

53.11 Pipeline code is reviewed like application code.

53.12 We clean up temporary datasets.

## 54. Testing Deep Dive

54.1 Unit tests cover logic in isolation.

54.2 Integration tests verify component interactions.

54.3 Contract tests protect API compatibility.

54.4 End-to-end tests cover critical journeys.

54.5 We test failure and error paths.

54.6 Tests are deterministic and independent.

54.7 We use test data factories, not production copies.

54.8 Coverage is measured and published.

54.9 Flaky tests are fixed or quarantined.

54.10 Tests run fast enough for feedback.

54.11 We review test quality like production code.

54.12 Performance and load tests guard the budget.

54.13 Accessibility tests run on key flows.

54.14 Test reports are accessible to the whole team.

## 55. Frequently Asked Questions

55.1 Q: How do I add a new endpoint? A: Follow the API style guide, add a contract, tests and observability.

55.2 Q: When do I need a new API version? A: Only for backward-incompatible changes; additive changes do not bump the version.

55.3 Q: How are secrets handled? A: Secrets live in the vault, referenced by environment variables, never committed.

55.4 Q: What is the pagination standard? A: Cursor-based pagination with a capped page size and a next_cursor field.

55.5 Q: How do I make an asynchronous call? A: Use the job pattern: enqueue, return a job id, poll the job status.

55.6 Q: How do services communicate? A: Prefer events over request-reply for cross-service coordination.

55.7 Q: What metrics must I expose? A: Rate, errors and duration for every endpoint, in Prometheus format.

55.8 Q: How do I correlate logs? A: Use the X-Request-Id and propagate trace context across services.

55.9 Q: When is a webhook needed? A: For outbound notifications; sign with HMAC and support replay.

55.10 Q: How are models deployed? A: Models are pinned and versioned; changes go through the release process.

55.11 Q: How do I test AI features? A: Use a versioned evaluation set and measure latency, cost and quality.

55.12 Q: Where do I find the design system? A: The internal design system is published on the intranet.

## 56. Release Management

56.1 Releases are built from a single pipeline.

56.2 Release artifacts are immutable and signed.

56.3 We deploy with automated gates and rollback capability.

56.4 Release notes are published with every release.

56.5 Canary releases reduce blast radius.

56.6 We follow a documented release calendar.

56.7 Feature flags decouple deployment from release.

56.8 Database migrations are coordinated with releases.

56.9 We verify the release in production after deploy.

56.10 Rollbacks follow the published runbook.

56.11 Release ownership is explicit per service.

56.12 We measure release frequency and change failure rate.

56.13 Emergency releases follow an expedited path.

56.14 Release readiness includes docs and observability.

## 57. Service Level Objectives

57.1 Every service defines availability and latency SLOs.

57.2 SLOs are documented and agreed with stakeholders.

57.3 We measure SLO attainment continuously.

57.4 Error budgets are defined for each SLO.

57.5 We alert on error-budget burn rate.

57.6 SLO violations trigger a review.

57.7 We publish SLO status internally.

57.8 SLOs account for dependency failures.

57.9 New services set provisional SLOs at launch.

57.10 SLOs are revisited as the service evolves.

57.11 We distinguish SLO from SLI definitions.

57.12 Dashboard and alerting reflect the SLO.

57.13 We communicate SLO changes to consumers.

57.14 SLO attainment is part of service reviews.

## 58. Engineering Metrics

58.1 We track deployment frequency per service.

58.2 Change lead time is measured from commit to deploy.

58.3 Change failure rate is monitored.

58.4 Mean time to recovery is tracked.

58.5 We measure availability against SLO.

58.6 Code review turnaround is visible.

58.7 We track test coverage trends.

58.8 Incident counts and severity are reviewed.

58.9 Developer satisfaction is surveyed.

58.10 We measure onboarding time for new engineers.

58.11 Technical debt is quantified where possible.

58.12 Metrics inform prioritisation, not punishment.

58.13 We review metrics quarterly.

58.14 Metrics are shared transparently.

## 59. Code Quality Standards

59.1 Code is formatted consistently with the team standard.

59.2 We enforce linting in CI.

59.3 Static analysis runs on every change.

59.4 Type checking is mandatory where the language supports it.

59.5 Code reviews check readability and maintainability.

59.6 We avoid dead code and unused dependencies.

59.7 Naming is clear and intention-revealing.

59.8 Functions are small and single-purpose.

59.9 We handle errors explicitly and early.

59.10 Tests are written for logic, not just coverage.

59.11 We document non-obvious decisions in comments.

59.12 Public APIs are documented and stable.

59.13 Code is reviewed by at least one peer.

59.14 We refactor continuously rather than postponing.

## 60. Collaboration and Communication

60.1 Decisions are documented in the repository.

60.2 We prefer written, async communication for decisions.

60.3 Meetings have agendas and outcomes.

60.4 We use the platform channels for cross-team coordination.

60.5 Technical proposals go through a lightweight ADR process.

60.6 We share learnings through internal talks and docs.

60.7 Feedback is specific, timely and respectful.

60.8 We assume positive intent and discuss openly.

60.9 On-call handovers are documented.

60.10 We celebrate team wins publicly.

60.11 Cross-team dependencies are surfaced early.

60.12 We keep stakeholders informed of progress.

60.13 Documentation is the default medium for decisions.

## 61. Change Management

61.1 Changes are described with intent and impact.

61.2 We review changes for security and compliance impact.

61.3 Database changes follow the migration policy.

61.4 We test changes against representative data.

61.5 Rollback plans accompany high-risk changes.

61.6 Change windows respect the service SLO.

61.7 We coordinate changes that affect shared resources.

61.8 Approval levels scale with change risk.

61.9 Change records are auditable.

61.10 We validate the change after deployment.

61.11 Emergency changes are documented retrospectively.

61.12 We review change outcomes periodically.

## 62. Capacity and Scalability

62.1 Capacity planning uses measured utilisation trends.

62.2 We scale out before scaling up where possible.

62.3 Autoscaling is configured with sensible bounds.

62.4 We monitor headroom against growth forecasts.

62.5 Load tests validate capacity assumptions.

62.6 We plan for peak and seasonal demand.

62.7 Database capacity is reviewed with data growth.

62.8 We document the scaling model per service.

62.9 Latency impact of scaling is measured.

62.10 We avoid over-provisioning beyond need.

62.11 Capacity reviews happen before major launches.

62.12 Degradation beyond capacity is graceful.

## 63. Data Privacy Engineering

63.1 Privacy requirements are considered at design time.

63.2 We minimise personal data by default.

63.3 Data flows are documented and reviewed.

63.4 We implement data subject rights programmatically.

63.5 Consent and lawful basis are recorded.

63.6 Access controls protect personal data.

63.7 We apply encryption to personal data at rest.

63.8 Retention limits are enforced for personal data.

63.9 Pseudonymisation is used where it fits.

63.10 We log access to personal data.

63.11 Privacy impact assessments run for high-risk processing.

63.12 Data breaches follow the incident process.

63.13 We redact personal data before external transfers.

63.14 Privacy engineering is reviewed in the security review.

## 64. Cloud Architecture Patterns

64.1 We design for horizontal scaling and statelessness.

64.2 Workloads are packaged as containers and run on the platform.

64.3 We use managed services to reduce operational burden.

64.4 Event-driven architectures decouple producers from consumers.

64.5 We adopt the strangler pattern for legacy migration.

64.6 Serverless is used where it fits the workload profile.

64.7 We prefer a microservice boundary when teams are separate.

64.8 Monoliths are acceptable until modularity demands more.

64.9 We design idempotent, retry-safe operations.

64.10 Circuit breakers protect critical dependencies.

64.11 We use the platform's golden-path templates.

64.12 Resilience is designed in, not retrofitted.

64.13 We apply the twelve-factor principles.

64.14 Architecture decisions are recorded as ADRs.

64.15 We avoid vendor lock-in where cost-effective.

64.16 Multi-region is a deliberate, justified choice.

64.17 We monitor architecture drift from the approved patterns.

## 65. Incident Post-Mortem Standards

65.1 Every major incident has a written post-mortem.

65.2 Post-mortems are blameless and fact-based.

65.3 We document the timeline of events.

65.4 We analyse root cause and contributing factors.

65.5 Action items are prioritised and owned.

65.6 We track action items to completion.

65.7 Post-mortems are shared with the team.

65.8 We avoid "human error" as a root cause.

65.9 We focus on systemic improvements.

65.10 Prevention is preferred over faster recovery.

65.11 We identify detection and response gaps.

65.12 Post-mortems inform runbooks and training.

65.13 We review whether alerts fired correctly.

65.14 We measure time to detection and recovery.

65.15 Post-mortem quality is reviewed periodically.

## 66. Engineering Handbook Quick Reference

66.1 New repo: use the platform template with CI, docs and observability.

66.2 New endpoint: follow the API style guide, add a contract and tests.

66.3 New database table: add a versioned migration and indexes.

66.4 New background job: use the job pattern with a queue.

66.5 New external integration: add timeout, retry and circuit breaker.

66.6 New secrets: store in the vault, reference by environment variable.

66.7 New metrics: expose rate, errors and duration in Prometheus format.

66.8 New alert: make it actionable, owned and tested.

66.9 New documentation: add it where engineers look, and keep it current.

66.10 New model: pin the version and evaluate on the eval set.

66.11 Code review checklist: correctness, security, observability, tests.

66.12 Release checklist: image built, tests green, rollback ready.

66.13 On-call checklist: runbooks, alerts, escalation path confirmed.

66.14 Compliance checklist: data residency, privacy, security review done.

66.15 Performance checklist: latency budget, load test, dashboards.

66.16 If in doubt, ask the platform team before proceeding.

66.17 Follow the golden path; document any exception as an ADR.

## 67. Database Performance Practices

67.1 We explain slow queries and fix the root cause.

67.2 Indexes are added only when measured queries need them.

67.3 We avoid functions on indexed columns in predicates.

67.4 Select only the columns you need.

67.5 We paginate large result sets.

67.6 Long transactions are split into smaller units.

67.7 We use batch inserts where appropriate.

67.8 Connection pool size is tuned per workload.

67.9 We avoid locking hot rows unnecessarily.

67.10 Read replicas absorb reporting load.

67.11 We cache hot reads where freshness allows.

67.12 Query plans are re-examined after schema changes.

67.13 We monitor buffer cache and I/O pressure.

67.14 Vacuum and maintenance follow the schedule.

67.15 We test performance on production-like data.

## 68. Frontend State Management

68.1 State is co-located with the component that owns it.

68.2 We lift state only when sharing requires it.

68.3 Global state is kept minimal and typed.

68.4 We derive values rather than storing duplicates.

68.5 Server state is cached and invalidated predictably.

68.6 We handle optimistic updates with rollback.

68.7 Loading and error states are first-class.

68.8 We avoid prop-drilling where context fits better.

68.9 State transitions are tested.

68.10 We persist only state that must survive reload.

68.11 Side effects are isolated and testable.

68.12 We keep state models close to the domain.

68.13 Global state libraries are used sparingly.

68.14 State bugs are tracked with the component tests.

## 69. Testing Pyramid Deep Dive

69.1 The pyramid favours many fast unit tests.

69.2 A smaller set of integration tests covers interactions.

69.3 A few end-to-end tests cover critical journeys.

69.4 We keep each layer focused and fast.

69.5 Unit tests mock at boundaries, not internally.

69.6 Integration tests use real components where practical.

69.7 We test contracts at the interface.

69.8 End-to-end tests run in stable environments.

69.9 We avoid brittle selectors in UI tests.

69.10 Tests are deterministic and repeatable.

69.11 We test failure modes, not just happy paths.

69.12 Coverage guides, but does not replace, good tests.

69.13 We fix flaky tests promptly.

69.14 The pyramid keeps the suite fast enough for CI.

## 70. Engineering Tools and Platforms

70.1 We use the platform's CI/CD for all services.

70.2 Source control follows the platform branching model.

70.3 Code search and review tools are mandated.

70.4 We use the internal package registry for libraries.

70.5 The design system is the source of frontend components.

70.6 Observability tools are centralised on the platform.

70.7 We use the platform's feature-flag service.

70.8 Secrets vault access is audited.

70.9 We standardise on the approved language toolchains.

70.10 Internal templates reduce boilerplate.

70.11 The intranet documents platform guidance.

70.12 We adopt new tools through a review process.

70.13 Tooling costs are attributed to teams.

70.14 We deprecate unused tools on a schedule.

## 71. Engineering Standards Checklist

71.1 Service naming follows the platform convention.

71.2 Every service has a README with run, test and operate instructions.

71.3 Health checks are exposed at GET /healthz.

71.4 Structured JSON logs carry the request id.

71.5 Prometheus metrics are exposed on a dedicated port.

71.6 Distributed tracing is propagated across calls.

71.7 Secrets are referenced from the vault, never committed.

71.8 Configuration is externalised and versioned.

71.9 Dependencies are pinned and scanned.

71.10 The service runs on Linux x86_64.

71.11 TLS is enforced for all external traffic.

71.12 Authorization is enforced per resource.

71.13 Input is validated at the boundary.

71.14 Error responses follow the standard shape.

71.15 Pagination uses cursors with a capped page size.

71.16 Idempotency keys are supported on mutating endpoints.

71.17 Long-running work uses the job pattern.

71.18 Outbound events use signed webhooks.

71.19 Database changes use versioned migrations.

71.20 Backups are tested regularly.

71.21 Feature flags control gradual rollouts.

71.22 Releases are immutable and rollback-able.

71.23 Performance budgets are defined and measured.

71.24 The observability checklist is complete before go-live.

71.25 The security review is complete before production.

71.26 Compliance and data residency are documented.

71.27 Documentation is current and linked from the alert.

71.28 On-call rotation and runbooks exist.

71.29 SLOs are defined and tracked.

71.30 The service is in the service catalogue.

71.31 Ownership and escalation are documented.

71.32 Incident response follows the runbook.

71.33 AI features are evaluated and grounded.

71.34 Model versions are pinned and monitored.

71.35 Personal data is minimised and protected.

71.36 Code is reviewed by at least one peer.

71.37 Tests are deterministic and pass in CI.

71.38 The golden path is followed; exceptions are ADRs.

71.39 Engineering standards apply to every deliverable.

71.40 When in doubt, ask the platform team.
