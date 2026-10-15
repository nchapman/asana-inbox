# Asana web-app Inbox protocol (reverse-engineered, 2026-10-15)

Undocumented; derived from a HAR of the web app. Expect breakage on Asana releases.

## Bootstrap (HTTP, session cookie)

Cookies used: `ticket` (CSRF token), `user` (user gid), `last_domain` (workspace gid).
Note user gid ≠ domain-user gid (the one in `/1/<domain>/inbox/<du>`).

1. App page HTML (`/1/<domain>/…`) contains `release_revision=<YYYYMMDD_HHMMSS_<commit>>`
   and the bundle URL `.../build/bundles/<commit>/apps/asana/bundle.js` (public CloudFront, ~46 MB).
2. `bundle.js` contains `{version:"<projectionBundleVersion>",clientQueries:...}`. Cache per commit.
3. `GET /app/asana/-/start_session?path=<url-path>&query_string=&user_id=<user>&optimistic_pageload=true
   &locale=en&release_revision=<rev>&shard_id=<domain>&guid=<uuid4>`
   with header `X-Asana-Ticket: <ticket cookie>` (without it: 403; POST: 400)
   → JSON `{session_id, app_data, luna_app_data}`.

## Websocket

`wss://cdn.sync.app.asana.com/socket?version=24&userId=<du>&shardId=<shard>&shardIdType=1&userLocale=en&allowedDomainsInfo={}`
with cookie + `Origin: https://app.asana.com`. JSON frames; server batches receives as arrays. DDP-like.

- send `{"msg":"connect","session":<session_id>,"projectionBundleVersion":…,"releaseRevision":…,"connectionCount":1}`
  → `{"msg":"connected",…}`
- server `ping` → client `pong`
- send `{"msg":"sub","id":"<n>","name":"<ProjectionName>","params":{…},"metadata":{…},"behavior":{…}}`
  where web app sends `metadata={idleTimeMs, isReconnect:false, isDuringPageLoad:false, loadsEssentialDataForPageLoad:true}`,
  `behavior={deferInvalidationReloadsWhenHidden:true, shouldDebugTrace:false}`.
  Range params elsewhere use `{"type":"RelativeRange","lower_delta":0,"lower_bound":{"type":"SpecialRank.Min"},
  "upper_bound":{"type":"SpecialRank.Min"},"upper_delta":N}`.
  → `added`/`changed`/`batch_removed` of `{collection, id, fields}`, then
  `{"msg":"subscription_state","id":"<n>","state":"ready"}`. The server also pushes its own sub
  (`id:"-1"`: Domain object + `ipIsAllowedAccess`) right after the first sub; ignore it.
- send `{"msg":"mutation","method":"/<name>","params":{"http_method":"post","endpoint_data":{…,"__app_name":"asana","__session_id":…}},"id":"<n>"}`
  → `committed`

Collections: `DbObject` (typed by `fields.typeName`), `ComputedObject`, `ServerComputedValue`, `ConfirmedQuery`, `Rank`.

## Listing the Inbox

Sub `CurrentDomainUserProjectionInbox` with `db_object_id=<du>` and `inboxThreadsInfo`:
`{showUnread:true, inboxTab:"activity", upperTimestampBound:8640000000000000, lowerTimestampBound:8640000000000000,
numAdditionalThreads:5, listThreadSize:3, multifilterParams:{objectViewId:…, multifilterList:[], inboxSortOverride:[{sortType:"priority",value:"recency"}]}}`
The minimal form (only `db_object_id` + `inboxThreadsInfo`) gets `{"msg":"error","reason":"Unexpected internal error"}`.
The full captured param set is in `SyncClient.sub_inbox` (`asana_web.py`); which keys are required is untested.
`currentDomainUserId` is sent as a number, `db_object_id` as a string; `objectViewId` is a per-user inbox view id.

- `ServerComputedValue ["currentDomainUserInboxThreads", du, {...}]` → `fields.value`: ordered `InboxThread/<threadId>|<suffix>` ids.
- `ComputedObject InboxThread/...` → `{notificationThread, threadObject, threadTypeName, notifications:[ids], isArchived}`.
- `DbObject` Notification → `{story, storyParent (task), notificationThread, unread}`.
- `DbObject` Task → `name`. `threadObject` is the Task, or a `Pot` (project) for e.g. `TaskAddedToPotNotificationThread`.
- Projects of a task: `ServerComputedValue ["taskProjectsContainingThis","<task>",{}]` → `value: [pot ids]`;
  `DbObject` Pot → `name`. All included in the inbox sub.
- Server echoes the resolved query as the `currentDomainUserInboxThreads` key, with `lowerTimestampBound`
  = oldest loaded thread (ms) and `numAdditionalThreads:0`. Pagination presumably re-subs with that
  bound + `numAdditionalThreads` — untested.

Thread shapes: `TaskNotificationThread` = several notifications about one task;
`TaskAddedToPotNotificationThread` (threadObject = Pot) = one notification per task added to that
project, i.e. several different tasks. Also `ConversationNotificationThread` (storyParent = Conversation;
its projects via Rank ids `Conversation.projectFollowers.<conv>.<pot>`), `PotNotificationThread`
(storyParent = Pot, e.g. ShareableSharedStory), `ActionRequestNotificationThread` (storyParent =
RequestShareableAccess `{state, requester, target, message}`), and storyParent = recipient DomainUser
(no subject): `DailySummaryNotificationThread` (DailySummaryStory.date) and `OverdueTasksReminderThread`
(OverdueTasksReminderStory.overdueTasksNotificationTimestamp).
Links: Asana rich text uses `/1/<domain>/project/<pot>/task/<task>` (PREFERRED_PATH); projects
`/1/<domain>/project/<pot>`; conversations legacy `/0/<pot>/<conv>`. All notification DbObjects of loaded threads are included (not just `listThreadSize`).
`numAdditionalThreads:50` returns 50 threads in one go. Without `objectViewId` the sub never became ready.

## Archiving

`/notification_set_unread_multi` with
`endpoint_data={domain, notifications:[<notification ids of the thread>], timestamp:<ms>, unread:false}`
plus `params.expensive_mutation=true` → `{"msg":"committed","id":…,"result":null}` then `{"msg":"reflected"}`.
Server then drops the thread from `currentDomainUserInboxThreads` (verified 2026-10-15).
Marking only some of a thread's notifications read (one task of a Pot thread) — untested.
