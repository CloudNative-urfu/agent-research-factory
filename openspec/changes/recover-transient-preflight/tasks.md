## 1. Reproduce
- [x] 1.1 Demonstrate stream finish error and missing durable retry with regression tests.
## 2. Implement
- [x] 2.1 Add stream diagnostics, transient classification, persisted waits and scoped recovery.
## 3. Verify
- [x] 3.1 Run checks and build deployment: 172 VM tests without skips, all Nix/OpenSpec checks and book build passed.
- [x] 3.2 Verify the updated probe against the live proxy: generation, JSON, tools and stream passed together.
- [ ] 3.3 After owner activation, confirm actual report worker invocation after a healthy preflight.
