# Scenario 3442510 — transform changelog

modules: 55 -> 42

- BUG-5: removed #210 http://thisurldoesnotexist.fail/stop hack (+ onerror Commit #215)
- BUG-2: removed 13 downstream WebhookRespond modules [9, 11, 13, 15, 24, 26, 28, 30, 37, 39, 43, 194, 237]
- BUG-2: inserted #251 early WebhookRespond {"status":"received"} (Content-Type JSON, 200) as flow[1]
- BUG-4: #5 filter -> 2-branch OR (name contains interest OR interest contains name), ci; dropped posted_to_website/rentable/visibility ANDs
- BUG-5: added util:SetVariable onerror (proven pattern) to modules [3, 5, 8, 10, 12, 14, 20, 23, 25, 27, 29, 31, 33, 36, 38, 40, 42, 189, 192, 195, 217, 234, 235]
- BUG-5: metadata.scenario.dlq=true (store incomplete executions); instant:true preserved
- SECURITY: redacted GHL_PRIVATE_TOKEN in 19 place(s) -> __GHL_PRIVATE_TOKEN__ (re-injected at push)
