# Bộ điều phối AI: Claude quản lý × Codex làm việc

`orch` là một công cụ dòng lệnh nhỏ viết bằng Python, chỉ dùng thư viện chuẩn, không cần cài thêm gì.

**Ý tưởng:**

- **Claude** đóng vai quản lý dự án. Claude viết yêu cầu ngắn, đọc báo cáo rút gọn và ra quyết định.
- **Codex** đóng vai kỹ sư. Codex đọc code, sửa code và chạy test.
- `orch` đứng giữa hai bên:
  - ghi lại trạng thái từng việc;
  - tự kiểm chứng kết quả của Codex (không tin lời Codex nói suông);
  - lưu bộ nhớ cho từng dự án, để lần sau không phải khảo sát lại toàn bộ repo.

## Tóm tắt trong 30 giây

1. Bạn nhờ Claude sửa lỗi hoặc thêm tính năng trong một dự án có git.
2. Claude **không tự đọc code**. Claude gọi `orch new …` để giao việc cho Codex.
3. Codex làm việc trong sandbox. `orch` dùng git để kiểm tra file nào thật sự thay đổi, rồi **tự chạy lại test**.
4. Kết quả được xử lý tuỳ mức rủi ro của việc:

   | Mức rủi ro | Kết quả |
   |---|---|
   | Thấp | Tự hoàn tất; Claude không tốn token review. |
   | Trung bình | Claude đọc báo cáo khoảng 300–500 token, rồi duyệt hoặc yêu cầu sửa. |
   | Cao | **Bạn** phải tự duyệt ở terminal trước khi Codex được làm. |

## Sơ đồ hoạt động

```
Bạn ─► Claude (quản lý) trong Claude Code / Claude desktop
         │ orch new: kiểm tra yêu cầu → phân loại rủi ro → chống giao trùng
         ▼
  ~/.ai-orchestrator/state.db: danh sách việc + nhật ký mọi lần đổi trạng thái
         │ orch run: khoá theo dự án → gọi codex exec (sandbox, chỉ ghi trong repo)
         ▼
  Codex CLI (dùng cấu hình ~/.codex và chính sách AGENTS.md)
         │ trả về JSON theo đúng mẫu + số token đã dùng
         ▼
  Cổng kiểm tra chất lượng:
    • git xác nhận file thay đổi       • có sửa ngoài phạm vi không?
    • orch tự chạy lại test bắt buộc    • Codex có báo "pass" sai sự thật không?
         ├─ rủi ro thấp + mọi thứ sạch ─► COMPLETED (tự hoàn tất)
         ├─ test fail ─► Codex tự sửa trên CÙNG thread (tối đa 3 vòng) ─► nếu vẫn fail: FAILED
         └─ còn lại ─► REVIEW_PENDING ─► Claude: duyệt / yêu cầu sửa / từ chối
  Bộ nhớ dự án <repo>/docs/ai/ + git checkpoint
```

## Bắt đầu nhanh

`orch` **đã được cài sẵn** trên máy bạn. Kiểm tra bằng lệnh:

```bash
orch doctor
```

Kết quả tốt sẽ có các dòng `codex login: Logged in…`, `policy block … yes` và `manager skill: yes`.

Từ giờ, khi bạn mở **bất kỳ session Claude mới nào** (Claude Code CLI hay Claude desktop), Claude tự biết phải giao việc code cho Codex qua `orch`. Bạn không cần làm gì thêm.

Muốn Claude tự làm trực tiếp cho một việc nào đó, chỉ cần nói rõ, ví dụ: *"lần này bạn tự sửa, đừng dùng orch"*.

## Sử dụng hằng ngày

Thường bạn chỉ cần nói chuyện với Claude, Claude sẽ tự gọi các lệnh này. Phần dưới đây để bạn hiểu chuyện gì đang diễn ra, hoặc tự chạy khi cần.

### Giao việc

Giao việc và chạy ngay:

```bash
orch new --objective "sửa add() để trả về tổng" --scope src/m.py --test "pytest -q tests/test_m.py" --run
```

- `--objective`: muốn đạt được điều gì.
- `--scope`: file hoặc thư mục dự kiến được sửa (lặp lại được). Phạm vi hẹp kèm có test thì việc được xếp **rủi ro thấp** và tự hoàn tất.
- `--test`: lệnh chứng minh việc đã xong. `orch` sẽ tự chạy lại lệnh này.
- `--risk medium|high`: nâng mức rủi ro nếu bạn thấy việc quan trọng hơn câu chữ thể hiện. Không thể hạ thấp mức rủi ro.
- `--run`: giao xong thì chạy ngay luôn.

### Xem kết quả

Xem báo cáo rút gọn của một việc:

```bash
orch report <mã-việc>
```

Báo cáo cho biết:

- trạng thái hiện tại;
- tóm tắt của Codex;
- file đã đổi (git xác nhận);
- test PASS hoặc FAIL do `orch` tự chạy lại;
- rủi ro;
- bước tiếp theo nên làm.

### Duyệt, yêu cầu sửa, từ chối (khi việc ở trạng thái REVIEW_PENDING)

Duyệt:

```bash
orch review <mã-việc> --approve --note "ổn"
```

Yêu cầu sửa thêm (Codex tiếp tục trên đúng thread cũ, giữ nguyên ngữ cảnh):

```bash
orch review <mã-việc> --fix "xử lý thêm trường hợp None"
```

Sau đó chạy lại:

```bash
orch run <mã-việc>
```

Từ chối:

```bash
orch review <mã-việc> --reject "sai hướng"
```

### Việc chạy lâu

Chạy nền. Tiến trình vẫn sống tiếp kể cả khi đóng session:

```bash
orch run <mã-việc> --detach
```

Chờ cho đến khi xong (mặc định tối đa 9 phút):

```bash
orch wait <mã-việc>
```

### Việc rủi ro cao (cần bạn duyệt)

Các việc liên quan production, deploy, migration, mật khẩu, quyền truy cập, thanh toán, xoá dữ liệu… sẽ dừng ở trạng thái `AWAITING_USER_APPROVAL`.

Bạn mở **Terminal** và chạy:

```bash
orch approve <mã-việc>
```

`orch` hiện báo cáo và yêu cầu bạn **gõ lại mã việc** để xác nhận.

Claude và Codex **không thể** tự duyệt thay bạn, vì shell của agent không có terminal thật.

Không muốn làm việc này nữa thì huỷ:

```bash
orch cancel <mã-việc>
```

### Dự án mới

Không cần cấu hình gì. Lần đầu dùng `orch new` trong một repo git (đã có ít nhất 1 commit), `orch` sẽ:

1. đăng ký dự án;
2. tạo thư mục `docs/ai/` (**không ghi đè** file bạn đã có);
3. ghi checkpoint git;
4. nhờ Codex khảo sát repo **một lần** để viết `PROJECT_CONTEXT.md` và `ARCHITECTURE_MAP.md`.

Repo chưa có git thì chạy `git init` và commit một lần trước.

### Tiếp tục công việc ở session mới

Mọi trạng thái được lưu trên đĩa, nên session mới tiếp tục được ngay.

Xem danh sách việc của dự án hiện tại:

```bash
orch status
```

Đọc thêm file bàn giao gần nhất: `docs/ai/LAST_HANDOFF.md`. Codex cũng tự đọc file này cùng danh sách file đã đổi kể từ checkpoint, nên chỉ phân tích phần liên quan thay vì đọc lại cả repo.

### Theo dõi

Việc trong dự án hiện tại:

```bash
orch status
```

Việc của mọi dự án:

```bash
orch status --all
```

Nhật ký đổi trạng thái của một việc (ai làm, lúc nào, vì sao):

```bash
orch log <mã-việc>
```

Bằng chứng chi tiết được lưu riêng, không đưa vào báo cáo:

```bash
ls ~/.ai-orchestrator/projects/<dự-án>/tasks/<mã-việc>/
```

Trong thư mục đó có:

- `run-N.jsonl`: log sự kiện của Codex;
- `diff.patch`: toàn bộ thay đổi;
- `tests.log`: kết quả `orch` chạy lại test;
- `result.json`: báo cáo gốc của Codex.

### Làm việc với dự án khác mà không cần `cd`

```bash
orch --path /đường/dẫn/tới/repo status
```

## Các trạng thái của một việc

Luồng bình thường:

`CREATED → TRIAGED → ASSIGNED → RUNNING → VALIDATING → REVIEW_PENDING → APPROVED → COMPLETED`

| Trạng thái | Nghĩa là | Bạn/Claude cần làm |
|---|---|---|
| `ASSIGNED` | đã giao, chưa chạy | `orch run <mã>` |
| `RUNNING` / `VALIDATING` | Codex đang làm / `orch` đang kiểm tra | `orch wait <mã>` |
| `REVIEW_PENDING` | chờ Claude xem xét | `orch review <mã> --approve` / `--fix` / `--reject` |
| `AWAITING_USER_APPROVAL` | rủi ro cao, chờ **bạn** duyệt | bạn chạy `orch approve <mã>` ở terminal |
| `RETRY_PENDING` | lỗi tạm thời hoặc đang chờ vòng sửa | `orch run <mã>` |
| `BLOCKED` | lỗi đăng nhập/model/quota, hoặc Codex cần quyết định | sửa nguyên nhân (`orch doctor`) → `orch review <mã> --fix "…"` → `orch run <mã>` |
| `FAILED` | đã hết 3 lần thử lại hoặc 3 vòng sửa, hoặc bị từ chối | đọc bằng chứng, tạo việc nhỏ hơn |
| `COMPLETED` | xong | không |
| `CANCELLED` | đã huỷ | không |

## Khi có sự cố

| Hiện tượng | Cách xử lý |
|---|---|
| Session Claude bị tắt giữa chừng khi việc đang `RUNNING` | Tự xử lý: lệnh `orch` kế tiếp phát hiện tiến trình đã chết và chuyển việc sang `RETRY_PENDING`. Chạy `orch run <mã>`, Codex tiếp tục trên thread cũ. |
| Báo `project busy` | Đang có việc khác chạy trong cùng repo. Đợi bằng `orch wait`, hoặc huỷ bằng `orch cancel`. |
| `BLOCKED` vì lỗi model hoặc đăng nhập | Chạy `orch doctor` để xem Codex còn đăng nhập không. |
| Codex chạy rất lâu | Mặc định timeout 30 phút. Quá thời gian, `orch` dừng Codex và thử lại tối đa 3 lần. |
| Muốn dừng ngay | `orch cancel <mã>` dừng cả tiến trình Codex. |

## Ba mức rủi ro

| Mức | Khi nào | Luồng xử lý |
|---|---|---|
| **Thấp** | có `--scope` hẹp, không có từ khoá nhạy cảm | Codex → kiểm tra → **tự hoàn tất**. Điều kiện: ít nhất 1 test bắt buộc PASS khi chạy lại, và không sửa ngoài phạm vi. |
| **Trung bình** | refactor, schema, database, API, config, nâng cấp, CI…, hoặc **không ghi `--scope`** | Codex → kiểm tra → Claude review |
| **Cao** | production, deploy, migration, mật khẩu, khoá API, quyền truy cập, đăng nhập, thanh toán, xoá dữ liệu… (nhận cả từ khoá tiếng Việt như "triển khai", "mật khẩu", "phân quyền", "thanh toán", "xóa dữ liệu") | **Bạn duyệt** → Codex → kiểm tra → Claude review |

## Cấu hình

Hai nơi cấu hình, dùng chung các khoá như nhau:

- **Toàn cục:** `~/.ai-orchestrator/config.json`.
- **Riêng từng dự án (tuỳ chọn):** `<repo>/.ai-orchestrator.json`, ghi đè cấu hình toàn cục cho dự án đó.

| Khoá | Mặc định | Ý nghĩa |
|---|---|---|
| `enabled` | `true` | `false` thì tắt điều phối, Claude tự làm trực tiếp |
| `codex_bin` | `"auto"` | dùng `codex` trong PATH; đặt đường dẫn tuyệt đối để ghim một bản cụ thể |
| `codex_model` | `null` | `null` thì dùng model trong `~/.codex/config.toml` |
| `codex_effort` | `{"low":"medium","medium":"high","high":"xhigh"}` | mức suy luận theo rủi ro, để việc nhỏ không chạy ở mức `ultra` tốn kém |
| `timeout_sec` | `1800` | thời gian tối đa cho mỗi lần Codex chạy |
| `test_timeout_sec` | `600` | thời gian tối đa cho mỗi lệnh test |
| `max_attempts` | `3` | số lần thử lại khi lỗi hạ tầng (chỉ được **giảm**, tối đa 3) |
| `max_auto_repair_attempts` | `3` | số vòng tự sửa khi test fail (chỉ được **giảm**, tối đa 3) |
| `memory_dir` | `"docs/ai"` | đặt đường dẫn tuyệt đối nếu không muốn ghi bộ nhớ vào repo (vẫn tách riêng từng dự án) |
| `extra_high_risk_keywords` / `extra_medium_risk_keywords` | `[]` | thêm từ khoá rủi ro của riêng bạn |

Những thứ **không thể tắt** bằng cấu hình:

- sandbox của Codex luôn là `workspace-write`, chỉ được ghi trong repo;
- việc rủi ro cao luôn cần bạn duyệt;
- mức rủi ro chỉ được nâng, không được hạ.

## Tắt, gỡ, khôi phục

1. **Tạm tắt:** sửa `"enabled": false` trong `~/.ai-orchestrator/config.json`. `orch new` sẽ từ chối, và Claude tự làm trực tiếp như trước.
2. **Gỡ cài đặt:** xoá các khối chính sách khỏi `~/.claude/CLAUDE.md` và `~/.codex/AGENTS.md`, xoá skill và lệnh tắt. **Giữ lại** lịch sử việc.
   ```bash
   orch uninstall
   ```
3. **Khôi phục hoàn toàn:** chép lại bản gốc từ `~/.ai-orchestrator/backups/`. Nếu không cần lịch sử nữa, tự xoá thư mục `~/.ai-orchestrator/`.

Cài lại từ thư mục mã nguồn (mỗi lần cài đều backup file bị chạm vào):

```bash
python3 orch.py install
```

`install` làm các việc sau:

- chép `orch` vào `~/.ai-orchestrator/bin/` và tạo lệnh tắt `~/.local/bin/orch`;
- tạo `config.json` nếu chưa có;
- cài skill `~/.claude/skills/orchestrate/` (chính sách đầy đủ cho Claude);
- thêm khối ngắn vào `~/.claude/CLAUDE.md`, có hiệu lực cho Claude Code CLI và Claude desktop;
- thêm khối chính sách vào `~/.codex/AGENTS.md`, có hiệu lực cho mọi session Codex.

## Đo token

Xem thống kê dự án hiện tại:

```bash
orch metrics
```

Thống kê gồm:

- số token Codex đã dùng;
- số lần giao việc;
- số vòng sửa;
- kết quả test;
- số lần khảo sát toàn repo;
- độ dài báo cáo trung bình.

Cộng thêm token phía Claude từ transcript của một session Claude Code:

```bash
orch metrics --claude-transcript ~/.claude/projects/<dự-án>/<session>.jsonl
```

**Kết quả đo thực tế (2026-10-09),** với một lỗi sửa 1 dòng:

| | Có điều phối | Claude tự làm |
|---|---|---|
| Chi phí Claude | $0.288 | $0.251 |
| Thời gian | 74 giây | 14 giây |
| Token Codex | ~95.000 đầu vào | 0 |

Với việc **rất nhỏ**, điều phối **tốn hơn** chứ không tiết kiệm. Lợi ích chỉ có thể xuất hiện ở những việc buộc Claude phải đọc nhiều file. Muốn biết chắc, hãy đo trên một việc thật trong dự án của bạn: làm một lần có `orch`, một lần không, rồi so sánh.

## Kết quả khảo sát môi trường (2026-10-08)

| Hạng mục | Tình trạng |
|---|---|
| `codex exec` (JSON, ép mẫu kết quả, tiếp tục thread) | Đã xác minh bằng chạy thật |
| Codex cài qua Homebrew (0.155.0) với model `gpt-6.1-sol` | **Không chạy được** (lỗi 400). Cần Codex bản mới hơn. |
| `codex exec` khi stdin chưa đóng | Bị treo. `orch` đã xử lý bằng cách đóng stdin. |

## Kiểm thử

Chạy bộ test (khoảng 25 giây, dùng Codex giả lập, không tốn token):

```bash
python3 test_orch.py
```

Bộ test bao gồm:

- tự hoàn tất với việc rủi ro thấp;
- review và vòng sửa;
- cổng duyệt của bạn;
- Codex báo test pass sai sự thật;
- dự án không nâng được giới hạn sửa;
- timeout rồi tiếp tục trên cùng thread;
- giới hạn thử lại;
- lỗi đăng nhập;
- sửa ngoài phạm vi;
- JSON hỏng;
- chống giao trùng;
- phục hồi khi session chết;
- huỷ việc;
- công tắc tắt;
- tiếp tục từ checkpoint;
- không ghi đè tài liệu có sẵn;
- cô lập giữa hai dự án;
- cài và gỡ.

Chạy thêm bài test với Codex thật trên hai dự án độc lập (vài phút, tốn token Codex):

```bash
ORCH_REAL=1 python3 test_orch.py RealCodex
```

## Giới hạn hiện tại

- Nguyên tắc "giao cho Codex trước" dựa vào chính sách trong CLAUDE.md và skill. Những phần **không thể lách** được:
  - bạn phải tự duyệt ở terminal;
  - mức rủi ro không hạ được;
  - giới hạn 3 lần thử lại và 3 vòng sửa;
  - `orch` tự chạy lại test.

  Ngoài những phần đó, Claude vẫn đọc file được nếu bạn yêu cầu.
- Phân loại rủi ro dựa trên từ khoá và phạm vi, có thể bỏ sót. Claude có thể nâng mức rủi ro khi cần.
- Mỗi repo chỉ chạy một việc tại một thời điểm. Muốn chạy song song, dùng `git worktree`; mỗi worktree được tính là một dự án riêng.
- `orch` **không bao giờ commit hay push**. Việc commit vẫn do bạn quyết định.
- Khối chính sách trong `~/.claude/CLAUDE.md` áp dụng cho **mọi** session Claude Code (CLI và desktop).
