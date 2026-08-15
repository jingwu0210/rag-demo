# API Specification

Version: v3.2 | Effective: 2025-04-01 | Classification: Internal

## Chapter 1: Authentication

All API requests must include a Bearer token in the Authorization header.
Tokens are issued by the identity service and expire after 24 hours.
Refresh tokens expire after 30 days.

## Chapter 2: Rate Limits

- Default rate limit: 1000 requests per minute per API key.
- Burst allowance: 1500 requests per minute for up to 60 seconds.
- Rate limit exceeded responses return HTTP 429 with Retry-After header.
- Rate limit counters reset at the top of each minute.

## Chapter 3: Error Codes

| Code | Meaning |
|------|---------|
| 400  | Invalid request parameters |
| 401  | Missing or expired token |
| 429  | Rate limit exceeded |
| 500  | Internal server error |
| 503  | Service temporarily unavailable |

## Chapter 4: Endpoints

### 4.1 GET /v3/users/{id}
Returns user profile. Requires scope `users:read`.

### 4.2 POST /v3/users
Creates a user. Requires scope `users:write`. Request body must be JSON.

### 4.3 GET /v3/health
Returns service health status. Public endpoint, no authentication required.
