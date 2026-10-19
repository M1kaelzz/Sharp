# 环境介绍
* 当前环境是授权渗透测试与安全验证工作区（web/API 精简镜像），内置常见 web 测试命令行工具。
* 当前目录可用于保存命令执行日志、扫描结果、验证证据、截图路径、临时脚本和报告素材。
* 所有操作必须围绕 Sharp 项目的 Origin、Goal、Hint 和当前 Intent 展开，不要越过授权范围。

# 工作原则
* 优先确认测试范围、目标资产、账号权限、时间窗口和禁止动作。
* 输出结论时区分 verified、suspected、todo、info，不要把未经验证的猜测写成已确认漏洞。
* 对每个高风险结论尽量保留可复测证据：请求/响应、命令输出摘要、受影响资产、复现条件、影响说明和修复建议。
* 没有明确请求/响应或命令证据时，不要编造 POC，只能给出验证方法和所需条件。
* 避免无意义的大范围破坏性操作；需要扫描时控制速率、范围和超时。

# 常用工具目录
* nuclei 模板：/home/kali/.local/nuclei-templates
* 常用安全工具：/home/kali/tools （jwt_tool、ysoserial.jar）
* **mitmproxy**：MITM 抓包/流量重放代理（`mitmweb` 有 Web UI，`mitmdump` 脚本化）
* **httpie**：REST API 测试客户端（比 curl 更直观，`--session` 跨请求保持 cookie/token）
* **session_manager.py**：多账号 HTTP 会话管理 + 并发 IDOR 扫描脚手架，路径 `/home/kali/tools/session_manager.py`
  ```python
  import sys; sys.path.insert(0, '/home/kali/tools')
  from session_manager import SessionManager
  sm = SessionManager.from_env()                         # 读 creds.env
  sm.add('bob', token='BOB_TOKEN', user_id='bob_uid')
  sm.print_compare(sm.compare('GET', 'https://target/api/order/123'))  # IDOR 对比
  sm.print_hits(sm.idor_scan('GET', 'https://target/api/order/{id}', range(1, 500)))
  print(sm.race('POST', 'https://target/api/coupon/use', data={'couponId': 'X'})['status_counts'])
  ```
* **crypto_helper.py**：加解密/签名/编码多合一，路径 `/home/kali/tools/crypto_helper.py`。目标请求体被 AES/DES/3DES/SM4 加密、参数带 MD5/SHA/HMAC/SM3 签名时用它脱密改包（前提：已从 JS/APP/配置里逆出 key/IV）。国密 SM2/SM3/SM4 全支持。
  ```bash
  # CLI：解 AES-CBC 密文（key/iv 用 hex，密文 base64）
  python3 /home/kali/tools/crypto_helper.py aes --op dec --mode cbc --key-hex 0011.. --iv-hex 00.. --in-b64 "U2Fsd..."
  # 改完明文重新加密发包
  python3 /home/kali/tools/crypto_helper.py sm4 --op enc --mode cbc --key-utf8 1234567890abcdef --iv-hex 00.. --in-utf8 '{"amount":1}'
  python3 /home/kali/tools/crypto_helper.py hmac --alg sm3 --key-utf8 secret --in-utf8 data   # 重算签名
  python3 /home/kali/tools/crypto_helper.py jwt --token eyJ...                                 # 解 JWT
  ```
  ```python
  import sys; sys.path.insert(0, '/home/kali/tools')
  from crypto_helper import aes_decrypt, sm4_encrypt, material
  pt = aes_decrypt(material(hexv='0011..'), material(b64='U2Fsd...'), mode='cbc', iv=material(hexv='00..'))
  ```
  注意：key/IV 必须显式给且长度正确，工具不做静默截断；成对 enc/dec 验证 roundtrip 再下结论。
* 安卓静态分析：/home/kali/tools/android
  - jadx（APK/DEX → Java，命令 `jadx`）、dex2jar（`d2j-dex2jar`，DEX → JAR）、vineflower（`$FERNFLOWER_JAR_PATH`，高质量 Java 反编译）
  - apktool（资源/AndroidManifest 解码）、adb（Android Debug Bridge）、frida / frida-tools（动态插桩客户端）
* 设备侧二进制：/home/kali/tools/device （panda-dex-dumper，ARM aarch64，脱壳用；仅 push 到手机执行，容器内不运行）

# 常见场景
* Web/API 测试：身份认证、权限控制、文件上传/读取、SQL 注入、SSRF、XXE、反序列化、命令执行、信息泄露、业务逻辑风险。
* 小程序测试：wxapkg 静态分析结果、接口路径、敏感配置、云函数/后端接口、登录态与鉴权逻辑；**HAR 动态流量导入**后可直接使用预提取的认证机制和 IDOR 候选参数清单（见下方「动态流量（HAR）测试」章节）。

# 业务逻辑漏洞测试

## 前提：获取测试凭证（必须先做）
* 拿到至少一个有效会话后，**立即将 token/cookie 写入文件**（如 `/home/kali/workspace/creds.env`），格式：
  ```
  TOKEN=eyJhbGci...
  USER_ID=12345
  ```
* 在 fact 结论中明确注明凭证位置和有效期，供后续 intent 复用。
* 凭证来源优先级：自注册测试账号 > 弱口令/默认凭证 > OAuth 公开流程 > hint 里提供的账号。
* 使用前先检查 `/home/kali/workspace/` 下已有文件，避免重复获取。

## 必须覆盖的测试维度（拿到 token 后）

### 1. 水平越权（IDOR）
遍历资源 ID，用账号 A 的 token 访问账号 B 的数据：
```bash
source /home/kali/workspace/creds.env
for id in $(seq 1 200); do
  code=$(curl -s -o /dev/null -w "%{http_code}" \
    -H "Authorization: Bearer $TOKEN" \
    "https://target/api/order/$id")
  echo "$id -> $code"
done | grep -v "^[0-9]* -> 403\|404"
```

### 2. 垂直越权
用低权限 token 访问管理/高权限接口：
```bash
source /home/kali/workspace/creds.env
for path in /api/admin /api/user/list /api/manage /api/config /api/system; do
  code=$(curl -s -o /dev/null -w "%{http_code}" -H "Authorization: Bearer $TOKEN" "https://target$path")
  echo "$path -> $code"
done
```

### 3. 业务流程绕过 / 参数篡改
跳步直接调流程末端接口；修改 price/amount/status 等业务参数测试服务端是否信任前端传值。

### 4. 竞态条件
```bash
seq 50 | xargs -P 50 -I{} curl -s -X POST "https://target/api/coupon/use" \
  -H "Authorization: Bearer $TOKEN" -d '{"couponId":"ABC123"}' \
  -o /dev/null -w "%{http_code}\n"
```

## 结论格式要求
* 每个维度说明：测试了哪些接口/ID范围、用了哪个账号的 token、响应差异。
* 确认越权时保留完整请求/响应对作为证据。
* 无法测试的维度注明原因，不要省略。
* 安卓 App 测试：server 已做浅层静态扫描（框架指纹、native 库、DEX 明文域名/接口/疑似密钥）。你在容器内的工作：
  - 先看 Origin 里的框架判定。Flutter / React Native / Xamarin / Unity 应用反编译 Java 价值有限，业务逻辑在 libapp.so / index.android.bundle / assemblies，改用对应工具（blutter、hermes-dec、strings 分析 .so）。
  - 原生 Java/Kotlin 应用：`jadx -d out app.apk` 反编译；输出少（疑似 split/bundle）时对内层 base.apk 再跑一次。
  - 结构分析：读 AndroidManifest.xml（组件、权限、launcher）、所有 BuildConfig.java（常泄露 baseURL/密钥/flag）、按 api/network/retrofit/http 包名定位接口。
  - 混淆 Kotlin：R8 无法剥离 Kotlin metadata，可从 @Metadata / @DebugMetadata 恢复真实类名再追调用链。
  - 提接口：优先 Retrofit 注解和硬编码 URL（永不被混淆）。产出「主机/方法/路径/鉴权/来源文件」清单，认证与支付流程重点展开。
* 组件风险：识别版本、配置、公开漏洞利用条件，并给出可复测的验证步骤。


# 动态流量（HAR）测试

当 fact 以「# 小程序动态流量（HAR）分析上下文」开头时，context 中已预提取三个关键节：**认证机制**（`primary` 字段）、**IDOR 越权候选参数**（对象 ID 参数清单）、**接口清单**（去重接口列表）。

## 1. 认证机制 → creds.env

`primary: bearer` → `Authorization: Bearer $TOKEN`；`cookie` → `-b "$COOKIE"`；`header:x-*` → 对应自定义名称。拿到实际值立即写入：
```bash
cat > /home/kali/workspace/creds.env << 'EOF'
TOKEN=eyJhbGci...   # 从 fact 或 hint 提取
USER_ID=12345
EOF
source /home/kali/workspace/creds.env
```

## 2. IDOR（直接使用候选参数清单，不要猜参数名）
```bash
source /home/kali/workspace/creds.env
# query 参数（location: query）—— 替换路径/参数名为清单中的实际值
for id in $(seq 1 300); do
  code=$(http -q --ignore-stdin GET "https://target/api/order/detail?orderId=$id" \
    "Authorization: Bearer $TOKEN" -o /dev/null --print h 2>&1 | grep -oP '\d{3}' | head -1)
  [ "$code" = "200" ] && echo "HIT $id"
done
# body-json 参数（location: body-json）
http --ignore-stdin POST https://target/api/user/info \
  "Authorization: Bearer $TOKEN" userId:=OTHER_USER_ID
```

## 3. 垂直越权（接口清单 → 筛管理类路径）
```bash
for p in /api/admin /api/manage /api/system /api/config /api/user/list; do
  echo -n "$p -> " && http -q --ignore-stdin GET "https://target$p" \
    "Authorization: Bearer $TOKEN" -o /dev/null --print h 2>&1 | grep -oP '\d{3}' | head -1
done
```

## 4. mitmproxy 重放 / httpie 多账号 session
```bash
# mitmproxy（Web UI: localhost:8081）
tmux new-session -d -s mitm "mitmweb --listen-port 8080"
curl -x http://127.0.0.1:8080 -sk -H "Authorization: Bearer $TOKEN" https://target/api/res/1
# httpie session — 自动持久化 cookie；多账号对比测 IDOR
http --session=/tmp/alice.json POST https://target/api/login username=alice password=p1
http --session=/tmp/alice.json GET  https://target/api/user/profile
http --session=/tmp/bob.json   POST https://target/api/login username=bob   password=p2
http --session=/tmp/alice.json GET  "https://target/api/user/detail?userId=BOB_ID"  # 期待403→IDOR
```

# 网络与监听
* 需要 OOB、回连、临时 HTTP 服务或 webhook 接收时，先确认项目授权和可用外联地址。
* 你当前容器里监听的端口是否可被目标访问，取决于运行环境的网络配置；不要默认可达。
* 如果需要记录监听或长时间任务，使用 tmux 并在结论中说明会话名、端口、保存路径和停止方式。


# 安卓静态分析

## APK 解包与反编译
```bash
# 原生 Java/Kotlin（最常见）
jadx -d /home/kali/workspace/out app.apk
apktool d app.apk -o /home/kali/workspace/apktool-out   # 组件/资源解码
```
先判断框架（避免对 Flutter/RN 做无效的 jadx 反编译）：
- `libflutter.so` + `libapp.so` → Flutter：改用 blutter / strings 分析 libapp.so
- `index.android.bundle` → React Native：hermes-dec 解码 hbc 字节码
- 无以上 lib，DEX 含 Retrofit/OkHttp → 原生 Java，jadx 全量反编译

## 分析顺序（原生 Java）
```bash
# 1. 组件与权限
cat /home/kali/workspace/apktool-out/AndroidManifest.xml | grep -A2 'activity\|service\|provider\|uses-permission'
# 2. 全局常量（最常泄露 baseURL / API 密钥）
find /home/kali/workspace/out -name "BuildConfig.java" -exec cat {} \;
# 3. Retrofit 接口定义文件清单
grep -rl "@GET\|@POST\|@PUT\|@DELETE\|@PATCH" /home/kali/workspace/out --include="*.java" | head -10
# 4. 硬编码 URL
grep -rh "https\?://" /home/kali/workspace/out --include="*.java" | sort -u | head -60
```

## 必须输出：接口清单文件

分析完毕后将接口写入 `/home/kali/workspace/apk-interfaces.json`，供后续 intent 直接读取，**不再重复反编译**：
```json
{
  "app_id": "com.example.app",
  "base_url": "https://api.example.com",
  "auth": {"type": "bearer", "obtain_via": "/user/login"},
  "interfaces": [
    {"method": "GET", "path": "/api/v1/user/{userId}", "risk": "idor_candidate"}
  ],
  "findings": [{"type": "hardcoded_key", "masked": "sk-ab...yz", "file": "BuildConfig.java"}]
}
```
在 fact 结论中明确注明文件路径，后续 intent 读取此文件做 API 和 IDOR 测试。

## 接口清单 → API 测试
```bash
BASE=$(python3 -c "import json;print(json.load(open('/home/kali/workspace/apk-interfaces.json'))['base_url'])")
# 批量未授权测试（200/302 → 未授权可达，401/403 → 需凭证）
jq -r '.interfaces[]|"\(.method) \(.path)"' /home/kali/workspace/apk-interfaces.json | \
  while read m p; do echo "$(curl -so/dev/null -w%{http_code} -X "$m" "$BASE$p") $m $p"; done | grep -v '^401\|^403'
# IDOR 扫描（risk=idor_candidate 的路径，配合 session_manager.py）
python3 -c "
import sys,json; sys.path.insert(0,'/home/kali/tools')
from session_manager import SessionManager
sm = SessionManager.from_env()
sm.print_hits(sm.idor_scan('GET', '$(python3 -c "import json;d=json.load(open(\"/home/kali/workspace/apk-interfaces.json\"));print(d[\"base_url\"]+d[\"interfaces\"][0][\"path\"])")', range(1,300)))
"
```

# 安卓设备（脱壳 / 动态插桩）
* 容器看不到 USB 设备。要操作 root 手机（脱壳、frida、抓包），必须走 TCP，不要假设 `adb -U` 能用：
  - 若环境变量 `ADB_SERVER_SOCKET` 已设置（形如 `tcp:host.docker.internal:5037`），说明宿主机在跑 adb server，直接 `adb devices` 即可看到设备。
  - 否则用无线 adb：`adb connect <手机IP>:<端口>`（手机需开无线调试）。
  - 连接失败时不要反复重试，在结论里说明「需要人工接入 root 手机 / 开启 adb」并继续能做的静态部分。
* 整体加固脱壳：`panda-dex-dumper`（ARM，设备侧二进制，在 /home/kali/tools/device）。流程：`adb push` 到 /data/local/tmp → chmod +x → 待 App 过启动页、壳解密后 → `./panda-dex-dumper -p $(pidof <包名>)` → dump 出的 dex 在 /data/local/tmp/panda/ → `adb pull` 回来 → 再用 jadx 反编译。需要 root（ptrace）。用完清理设备上的文件。
* 动态插桩：容器内已装 frida 客户端；设备侧需自备匹配版本的 frida-server（ARM）。容器无 USB，用远程模式 `frida -H <手机IP或转发端口>:27042`，不要用 `frida -U`，也不要用已废弃的 `--no-pause`。
* 所有针对设备的操作同样受 Origin/Goal 授权范围约束，只测授权目标。

# 其他
* 可以直接尝试常见工具命令，例如 nuclei、katana、httpx、naabu、ffuf、dalfox、nikto、nmap、curl、jq、python 等。
* 安卓静态分析工具：jadx、d2j-dex2jar、vineflower（jar 在 $FERNFLOWER_JAR_PATH）、apktool、adb、frida。
* 长时间运行的扫描、代理、监听服务应放在 tmux 会话中运行，并定期检查输出。
* 最终写回 Sharp 的结论应简洁、可复核，长日志保存为文件并在结论中引用路径。
