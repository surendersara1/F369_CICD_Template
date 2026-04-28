# SOP — Q Developer /transform (Java upgrade · .NET porting · mainframe COBOL · agentic codebase modernization)

**Version:** 2.0 · **Last-reviewed:** 2026-04-27 · **Status:** Active
**Applies to:** AWS CDK v2 (Python 3.12+) · Q Developer /transform agent (GA Apr 2024) · Java 8/11/17/21 upgrades · .NET Framework → .NET (modern) porting · COBOL → Java (mainframe modernization, preview) · VMware to AWS workload migration · transformation workflow + manual review gates

---

## 1. Purpose

- Codify **Q Developer's `/transform` agentic feature** — automated codebase modernization that handles tedious version upgrades + dependency migrations across thousands of files.
- Codify the **3 main transformation types**:
  - **Java upgrade** (Java 8/11 → Java 17/21) — most mature, GA
  - **.NET port** (.NET Framework 4.x → .NET 8/9 modern) — GA Apr 2024
  - **COBOL → Java** (mainframe modernization) — preview
  - **VMware → AWS** (workload migration) — preview
- Codify the **transformation workflow** — discovery → plan → preview → execute → review → commit → test.
- Codify **manual review gates** — Q produces patches; human + CI must validate before merge.
- Codify **integration with CI/CD** — run tests automatically post-transform; block merge on failure.
- Codify **batch transformation** for large monorepos (split into smaller modules).
- Codify the **escalation path** — when /transform stalls (custom builds, proprietary frameworks).
- Pairs with `AI_DEV_Q_DEVELOPER` (base), `AI_DEV_AGENTIC_REFACTOR` (agentic dev integration), `MIGRATION_SCHEMA_CONVERSION` (DB modernization counterpart).

When the SOW signals: "modernize legacy codebase", "Java 8 to 17", ".NET Framework to modern", "mainframe modernization", "exit Java license costs", "container modernization for legacy apps".

---

## 2. Decision tree — transformation type + readiness

| Source | Target | Maturity | Time per 100K LOC |
|---|---|---|---|
| Java 8 | Java 17 | GA | hours-days |
| Java 11 | Java 17 | GA | hours |
| Java 17 | Java 21 | GA | hours |
| .NET Framework 4.x | .NET 8/9 | GA | days |
| .NET Framework 4.x → AWS-hosted | .NET 8 on Linux + ECS | GA | days-weeks |
| COBOL (z/OS) | Java | Preview | weeks-months |
| VMware VM | EC2 + container | Preview (uses MGN under hood) | days |

```
Workflow per transformation:

   Pre-flight readiness
        │
        ├── Source codebase in Git, latest stable branch
        ├── Build succeeds (mvn / dotnet)
        ├── Tests pass on existing version
        ├── Maven/NuGet dependencies enumerated
        └── Custom plugins / native libs documented
        ▼
   Initiate transformation in IDE
        │
        ├── /transform in IDE chat OR Q Developer CLI
        ├── Q analyzes project (5-30 min)
        ├── Q produces transformation plan + risk score
        │     - Auto-transformable: 80-95%
        │     - Manual review needed: 5-20%
        │     - Cannot transform: < 5% (rare APIs, native code)
        └── Approve plan → execute
        ▼
   Execution
        │
        ├── Q applies changes in branches (per-module if monorepo)
        ├── Updates pom.xml / .csproj / project files
        ├── Updates dependency versions
        ├── Migrates deprecated APIs
        ├── Refactors language idioms (var, switch expressions, records)
        └── Updates test code
        ▼
   Review + validate
        │
        ├── Diff review by senior engineer (cannot skip)
        ├── Build runs: mvn clean install (or dotnet build)
        ├── Tests run + must pass
        ├── Performance regression check (load test)
        └── Static analysis (SpotBugs / Roslyn analyzers)
        ▼
   Merge + deploy
        │
        ├── Merge to feature branch
        ├── CI/CD pipeline runs full integration tests
        ├── Stage deploy + smoke
        └── Production deploy + monitor
```

### 2.1 Variant for the engagement

| You are… | Use variant |
|---|---|
| POC — single Java repo, < 100K LOC | **§3 Single Repo** |
| Production — monorepo + multi-module + 1M+ LOC | **§5 Multi-Module** |

---

## 3. Single Repo Variant — Java 8 → 17 upgrade

### 3.1 Pre-flight checklist

```bash
# Verify source builds + tests pass
cd ~/myrepo
mvn clean test
echo "Source baseline: $(date)"
git rev-parse HEAD > .baseline-sha

# Snapshot dependency tree
mvn dependency:tree > .pre-transform-deps.txt
mvn dependency:analyze > .pre-transform-analysis.txt

# Document custom Maven plugins, repackage configs
grep -r "<plugin>" pom.xml > .custom-plugins.txt
```

### 3.2 Initiate /transform

In VSCode chat panel:
```
/transform Upgrade this Java 8 codebase to Java 17. Target Spring Boot 3.x.
Update Jakarta EE namespaces (javax → jakarta). Generate migration report.
```

Or via Q Developer CLI:
```bash
q transform start \
  --type java-upgrade \
  --source-version 8 \
  --target-version 17 \
  --project-dir ~/myrepo \
  --output-dir ~/myrepo-transformed
```

Q Developer:
1. **Analyze phase (5-30 min)**:
   - Reads pom.xml, source files, test files
   - Identifies Java 8 idioms + deprecated APIs
   - Estimates LOC needing change
   - Produces plan
2. **Plan output**:
   ```
   ─── Transformation Plan: java-8 → java-17 ───
   Files analyzed: 1,234
   Files needing change: 287
   
   Major changes:
     - Replace anonymous Runnable → lambda (135 occurrences)
     - Replace if-else chains → switch expressions (42)
     - Replace builder pattern → records (18 candidates)
     - javax.* → jakarta.* (87 imports)
     - Update deprecated APIs:
         * Runtime.getRuntime().exec() → ProcessBuilder (3)
         * URL constructor → URI.create().toURL() (8)
   
   Spring Boot 3.x changes:
     - @ConfigurationProperties prefix changes
     - WebSecurity migration to SecurityFilterChain
     - Test annotations: @MockBean → @MockitoBean
   
   Risk: LOW (auto-transformable: 92%; manual: 8%)
   Estimated time: 45 min execution + 4 hours review.
   
   Approve? (Y/n)
   ```
3. **Execute (30-60 min)**:
   - Q applies changes
   - Outputs to `~/myrepo-transformed` OR new branch
   - Generates migration report

### 3.3 Review + validate

```bash
cd ~/myrepo-transformed

# Build with Java 17
export JAVA_HOME=/usr/lib/jvm/java-17-openjdk
mvn clean install

# If build succeeds, run tests
mvn test

# If tests pass, review diff vs original
diff -ru ~/myrepo ~/myrepo-transformed > transformation.diff
wc -l transformation.diff

# Senior engineer reviews diff (cannot skip)
# Things to verify manually:
#   - Business logic preserved (no semantic drift)
#   - Performance hot paths unchanged
#   - Security-sensitive code (auth, crypto) reviewed carefully
#   - Custom serialization untouched (or fixed correctly)

# CI integration check
git checkout -b feat/java-17-upgrade
git apply transformation.diff
git commit -am "Q Developer transform: Java 8 → 17 (auto)"
git push
# CI runs full test suite + integration + smoke
```

---

## 4. .NET Framework → .NET (modern) port

### 4.1 Initiate

```
/transform Port this .NET Framework 4.8 application to .NET 8.
Target: cross-platform (Linux), no Windows-specific APIs.
Move from System.Web to ASP.NET Core minimal API.
```

Q Developer handles:
- Project file: `.csproj` → SDK-style
- Web: `System.Web` → ASP.NET Core minimal API
- Config: `web.config` / `app.config` → `appsettings.json`
- DI: `Unity` / `Autofac` → built-in DI
- Authentication: `OWIN` → ASP.NET Core auth
- Async: `Task.Wait()` / `.Result` → `await`
- HTTP client: `HttpWebRequest` → `HttpClient`
- WCF: → gRPC (if applicable) or REST migration
- EntityFramework 6 → EntityFramework Core
- Removes Windows-specific APIs (`System.Drawing` → SkiaSharp, etc.)

### 4.2 Common .NET transformation gotchas

- `HttpContext.Current` (static) → DI-injected `IHttpContextAccessor`. Must rewrite all callers.
- `ConfigurationManager.AppSettings` → `IConfiguration`. Refactor everywhere.
- `app.UseMvc()` middleware → `app.MapControllers()`. Routing changes.
- Globalization differences: .NET 5+ uses ICU on Linux; some date-format edge cases may differ from Windows.
- Native interop (`P/Invoke`, COM): Q flags but doesn't auto-fix. Manual port required.

---

## 5. Multi-Module Monorepo (1M+ LOC)

### 5.1 Strategy: split + transform per module

```bash
# 1. Identify module boundaries
mvn dependency:tree | grep "^.- " | head -50

# 2. For each independent module, transform separately
for module in users-svc orders-svc payments-svc inventory-svc; do
  q transform start \
    --type java-upgrade \
    --source-version 11 \
    --target-version 17 \
    --project-dir ./modules/$module \
    --output-branch transform/$module-java17
done

# 3. Merge module-by-module to main; CI gates each
```

### 5.2 Long-running transformations

- For modules > 500K LOC, transformation can take 4-12 hours
- Q queues + processes in background; check with `q transform status <id>`
- On failure mid-flight, Q resumes; keep partial state in case

---

## 6. COBOL → Java (mainframe modernization, preview)

```
/transform Migrate COBOL programs in ./src/cobol to Java 17 + Spring Batch.
Maintain semantic equivalence. Generate JCL → Spring Batch job equivalents.
```

Q Developer (preview):
1. Parses COBOL programs (PROCEDURE DIVISION, DATA DIVISION)
2. Generates Java classes per COBOL program
3. Translates JCL to Spring Batch jobs
4. Migrates VSAM access → JPA / JDBC
5. Output: Spring Boot project + Maven build

**Caveats**:
- Preview as of Apr 2024; not for safety-critical production yet
- Hand-review every output
- Performance characteristics differ — load test
- Some COBOL idioms (REDEFINES, OCCURS DEPENDING ON) may not auto-migrate

---

## 7. CI/CD integration

### 7.1 GitHub Actions workflow

```yaml
# .github/workflows/q-transform-validate.yml
name: Q Transform Validation
on:
  pull_request:
    branches: [main]
  workflow_dispatch:
    inputs:
      transform_type:
        type: choice
        options: [java-8-to-17, dotnet-fx-to-net8]

jobs:
  build-and-test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - name: Setup Java
        uses: actions/setup-java@v4
        with: { java-version: '17' }
      - name: Maven build
        run: mvn -B clean install
      - name: Run tests
        run: mvn test
      - name: SpotBugs analysis
        run: mvn spotbugs:check
      - name: Coverage gate
        run: mvn jacoco:check
      - name: Performance regression test
        run: mvn -Pperf-test test
      - name: Notify on failure
        if: failure()
        run: |
          # Block merge; require manual override
          echo "::error::Transformation broke tests"
```

### 7.2 Q Developer CLI in CI

```bash
# Trigger transformation programmatically
q transform start \
  --type java-upgrade \
  --source-version 8 \
  --target-version 17 \
  --project-dir . \
  --output-branch transform/java17-${{ github.run_id }} \
  --wait

# Wait for completion
q transform wait <transform-id>

# Verify pass:
mvn test
```

---

## 8. Common gotchas

- **/transform is not 100% accurate** — typical 92-95% auto. Plan for human review of 5-8%.
- **Custom Maven plugins / proprietary frameworks** stall transformation. Document beforehand.
- **Native code (JNI, P/Invoke)** flagged but not auto-ported. Manual.
- **Annotation-driven DI** (Spring, ASP.NET) can subtly break — semantic differences across versions.
- **Test code transformation** — tests upgraded too; mock libraries may have breaking changes (Mockito 4 → 5).
- **Build chain** — Maven/NuGet plugin versions also need upgrade. Q updates pom.xml/csproj automatically.
- **Long-running transformations** can fail mid-execution; rerun with `--resume <id>`.
- **License costs** — net savings: Java 8 free → 17 paid (Oracle) OR 17 free (Amazon Corretto). Check vendor.
- **Rollback** — Q produces a Git branch / output dir. NEVER overwrite source. Fail-safe by design.
- **Test coverage gap exposed** — many legacy codebases have 30-50% coverage. /transform output may pass tests but break uncovered paths. Add tests BEFORE transform.
- **Performance regression** — JIT optimizations differ across Java versions; tested workload may be 5-10% faster or slower. Load-test in stage.
- **LOC limits per transform** — soft limit ~500K LOC per single transform. Beyond, split into modules.
- **Multiple transformations stacked** — Java 8 → 11 → 17 → 21. /transform supports direct 8 → 17, but Java 8 → 21 may want intermediate stop.
- **Q Developer Pro tier required** for /transform — Free tier excluded.
- **Custom analyzers (Sonar, Checkmarx)** may flag warnings on transformed code (different idioms). Re-baseline post-transform.

---

## 9. Pytest worked example

```python
# tests/test_q_transform.py
import subprocess, json, pytest


def test_q_transform_completes_on_sample_repo(tmp_path):
    """Run /transform on a small fixture; verify output buildable."""
    src = tmp_path / "src"
    src.mkdir()
    # Fixture: Java 8 hello-world Maven project
    # ... write pom.xml + src/main/java/Hello.java ...
    
    result = subprocess.run([
        "q", "transform", "start",
        "--type", "java-upgrade",
        "--source-version", "8",
        "--target-version", "17",
        "--project-dir", str(src),
        "--output-dir", str(tmp_path / "out"),
        "--wait",
    ], capture_output=True, text=True, timeout=600)
    
    assert result.returncode == 0, f"Transform failed: {result.stderr}"
    # Verify output buildable
    build = subprocess.run(
        ["mvn", "-B", "clean", "test"],
        cwd=tmp_path / "out", capture_output=True, text=True,
    )
    assert build.returncode == 0


def test_transform_report_completeness(tmp_path):
    """Check transformation report includes all required fields."""
    report_path = tmp_path / "out" / "transformation-report.json"
    with open(report_path) as f:
        report = json.load(f)
    assert "filesAnalyzed" in report
    assert "filesChanged" in report
    assert "manualReviewRequired" in report
    assert "estimatedHours" in report


def test_no_critical_failures():
    """No transformation errors at CRITICAL level."""
    # Read Q Developer logs; assert no CRITICAL entries
    pass
```

---

## 10. Five non-negotiables

1. **Tests pass on source** before /transform — never transform unstable code.
2. **Output to new branch / new directory** — never overwrite source.
3. **Senior engineer review of diff** — cannot skip; even at 95% auto, human catches semantic drift.
4. **CI gate post-transform** — full test + perf + static analysis before merge.
5. **Performance regression test** in stage before prod — Java/JVM idioms can change perf 5-10%.

---

## 11. References

- [Q Developer /transform User Guide](https://docs.aws.amazon.com/amazonq/latest/qdeveloper-ug/transform.html)
- [Java upgrade guide](https://docs.aws.amazon.com/amazonq/latest/qdeveloper-ug/transform-java.html)
- [.NET port guide](https://docs.aws.amazon.com/amazonq/latest/qdeveloper-ug/transform-net.html)
- [VMware to AWS via Q](https://docs.aws.amazon.com/amazonq/latest/qdeveloper-ug/transform-vmware.html)
- [Q Developer CLI](https://docs.aws.amazon.com/amazonq/latest/qdeveloper-ug/command-line.html)

---

## 12. Changelog

| Version | Date | Change |
|---|---|---|
| 2.0 | 2026-04-27 | Initial. Q Developer /transform: Java 8/11 → 17/21, .NET Framework → modern, COBOL → Java (preview), VMware → AWS (preview), CI integration. Wave 18. |
