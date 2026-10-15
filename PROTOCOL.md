# Asana web-app Inbox protocol (reverse-engineered, 2026-10-15)

Undocumented; derived from a HAR of the web app. Expect breakage on Asana releases.

## Bootstrap (HTTP, session cookie)

1. `GET https://app.asana.com/app/asana/-/start_session` → JSON `{session_id, app_data, luna_app_data}`.
2. Inbox page HTML (`/1/<domain>/inbox/<domain_user>`) contains `release_revision=<YYYYMMDD_HHMMSS_<commit>>`
   and the bundle URL `.../build/bundles/<commit>/apps/asana/bundle.js` (public CloudFront, ~46 MB).
3. `bundle.js` contains `{version:"<projectionBundleVersion>",clientQueries:...}`. Cache per commit.

## Websocket

`wss://cdn.sync.app.asana.com/socket?version=24&userId=<du>&shardId=<shard>&shardIdType=1&userLocale=en&allowedDomainsInfo={}`
with cookie + `Origin: https://app.asana.com`. JSON frames; server batches receives as arrays. DDP-like.

- send `{"msg":"connect","session":<session_id>,"projectionBundleVersion":…,"releaseRevision":…,"connectionCount":1}`
  → `{"msg":"connected",…}`
- server `ping` → client `pong`
- send `{"msg":"sub","id":"<n>","name":"<ProjectionName>","params":{…},"metadata":{…},"behavior":{…}}`
  → `added`/`changed`/`batch_removed` of `{collection, id, fields}`, then `subscription_state`
- send `{"msg":"mutation","method":"/<name>","params":{"http_method":"post","endpoint_data":{…,"__app_name":"asana","__session_id":…}},"id":"<n>"}`
  → `committed`

Collections: `DbObject` (typed by `fields.typeName`), `ComputedObject`, `ServerComputedValue`, `ConfirmedQuery`, `Rank`.

## Listing the Inbox

Sub `CurrentDomainUserProjectionInbox` with `db_object_id=<du>` and `inboxThreadsInfo`:
`{showUnread:true, inboxTab:"activity", upperTimestampBound:8640000000000000, lowerTimestampBound:8640000000000000,
numAdditionalThreads:5, listThreadSize:3, multifilterParams:{objectViewId:…, multifilterList:[], inboxSortOverride:[{sortType:"priority",value:"recency"}]}}`
(full captured params are large; most are UI-only and may be droppable — untested).

- `ServerComputedValue ["currentDomainUserInboxThreads", du, {...}]` → `fields.value`: ordered `InboxThread/<threadId>|<suffix>` ids.
- `ComputedObject InboxThread/...` → `{notificationThread, threadObject, threadTypeName, notifications:[ids], isArchived}`.
- `DbObject` Notification → `{story, storyParent (task), notificationThread, unread}`.
- `DbObject` Task → `name`. No project membership seen on Task; `threadObject` may be a `Pot` (project).
- Pagination: presumably via `numAdditionalThreads` / timestamp bounds — untested.

## Archiving

`/notification_set_unread_multi` with
`endpoint_data={domain, notifications:[<notification ids of the thread>], timestamp:<ms>, unread:false}`
plus `params.expensive_mutation=true`. Server then drops the thread from `currentDomainUserInboxThreads`.
