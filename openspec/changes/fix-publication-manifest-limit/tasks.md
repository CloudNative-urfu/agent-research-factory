## 1. Reproduce and fix
- [x] 1.1 Reproduce the >2 MB manifest failure and aggregate-metadata gap with tests.
- [x] 1.2 Implement metadata-specific limits, hash checks and complete aggregate validation.
## 2. Verify and prepare recovery
- [x] 2.1 Run regressions, strict OpenSpec and Nix checks; export revision 8 to a temporary directory.
- [x] 2.2 Document activation and narrowly scoped pause recovery; build book with the corrected input.

Verification: 165 tests in VM without skips; all Nix checks and strict OpenSpec pass.
Isolated revision-8 export: 10,447,913 bytes; source manifest hash unchanged; repeat hash
218f2c94b0e8a27e6347550c2c5bd8545f887d8f80cbf1911314bf0fd54a3abb matches.
Live state was not edited; the migration runs after the owner activates the new package.
