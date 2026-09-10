"""OpenAI 兼容 LLM 客户端：纯标准库实现（urllib），支持重试、JSON 输出模式。

POST {base_url}/chat/completions
"""
import json
import time
import urllib.error
import urllib.request


class LLMError(Exception):
    pass


class LLMClient:
    def __init__(self, api_key: str, base_url: str, model: str,
                 temperature: float = 0.2, max_tokens: int = 8192,
                 timeout: int = 180, max_retries: int = 4, thinking: bool = False):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.timeout = timeout
        self.max_retries = max_retries
        self.thinking = thinking
        # token 用量统计
        self.total_prompt_tokens = 0
        self.total_completion_tokens = 0
        self.total_requests = 0

    def usage_report(self) -> str:
        """返回 token 用量统计文本。"""
        t = self.total_prompt_tokens + self.total_completion_tokens
        return (f"请求 {self.total_requests} 次 | prompt {self.total_prompt_tokens:,} "
                f"+ completion {self.total_completion_tokens:,} = 共 {t:,} tokens")

    # ---------- 基础请求 ----------
    def chat(self, messages, json_mode: bool = False, temperature: float = None,
             max_tokens: int = None) -> str:
        """发送对话请求，返回助手回复文本。自动重试（429/5xx/网络错误）。"""
        url = f"{self.base_url}/chat/completions"
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature if temperature is None else temperature,
            "max_tokens": self.max_tokens if max_tokens is None else max_tokens,
            "stream": False,
        }
        # 禁用思考（reasoning）：SiliconFlow/DeepSeek 系模型默认开启思考，
        # 翻译任务开启思考会大幅拖慢速度且对质量帮助有限。
        if not self.thinking:
            payload["thinking"] = {"type": "disabled"}
        if json_mode:
            payload["response_format"] = {"type": "json_object"}

        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            url, data=body,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
        )

        last_err = None
        for attempt in range(self.max_retries + 1):
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                if not data.get("choices"):
                    last_err = f"empty choices: {json.dumps(data)[:300]}"
                    retryable = True
                elif not (data["choices"][0]["message"]["content"] or "").strip():
                    last_err = "empty content (possibly max_tokens too small)"
                    retryable = True
                else:
                    # 统计 token 用量
                    usage = data.get("usage", {})
                    self.total_prompt_tokens += int(usage.get("prompt_tokens", 0))
                    self.total_completion_tokens += int(usage.get("completion_tokens", 0))
                    self.total_requests += 1
                    return data["choices"][0]["message"]["content"]
            except urllib.error.HTTPError as e:
                err_body = e.read().decode("utf-8", "replace")[:500]
                last_err = f"HTTP {e.code}: {err_body}"
                retryable = e.code in (408, 429, 500, 502, 503, 504)
                if not retryable:
                    raise LLMError(last_err) from e
            except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
                last_err = f"network: {e}"
                retryable = True
            except (KeyError, json.JSONDecodeError) as e:
                raise LLMError(f"bad response: {e}") from e

            if attempt < self.max_retries:
                time.sleep(1.5 * (2 ** attempt))  # 指数退避
        raise LLMError(f"retries exhausted: {last_err}")

    # ---------- 便捷方法 ----------
    def chat_json(self, messages, temperature: float = None, max_tokens: int = None) -> dict:
        """要求模型输出 JSON 对象，解析后返回 dict。失败抛 LLMError。"""
        text = self.chat(messages, json_mode=True, temperature=temperature,
                         max_tokens=max_tokens)
        return parse_json(text)

    def chat_text(self, prompt: str, system: str = None) -> str:
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        return self.chat(messages)


def parse_json(text: str) -> dict:
    """解析模型输出中的 JSON。容忍 ```json 围栏、前后杂质、截断。

    失败时尝试多种修复：截取 {..} 范围、截断补全（引号/括号）、单引号替换。
    """
    t = text.strip()
    # 去掉围栏
    if t.startswith("```"):
        t = t.strip("`")
        if t.startswith("json"):
            t = t[4:]
        t = t.strip()
    candidates = [t]
    # 截取第一个 { 到最后一个 }
    s, e = t.find("{"), t.rfind("}")
    if s != -1 and e > s:
        candidates.append(t[s:e + 1])
    # 截断修复后的变体
    repaired = _repair_truncated_json(t)
    if repaired != t:
        candidates.append(repaired)
        s2, e2 = repaired.find("{"), repaired.rfind("}")
        if s2 != -1 and e2 > s2:
            candidates.append(repaired[s2:e2 + 1])
    for cand in candidates:
        try:
            return json.loads(cand)
        except json.JSONDecodeError:
            continue
    # 最后手段：单引号替换为双引号（模型偶尔用单引号）
    if "'" in t:
        t2 = t.replace("'", '"')
        s3, e3 = t2.find("{"), t2.rfind("}")
        if s3 != -1 and e3 > s3:
            try:
                return json.loads(t2[s3:e3 + 1])
            except json.JSONDecodeError:
                pass
    raise LLMError(f"cannot parse JSON from model output: {text[:500]!r}")


def _repair_truncated_json(t: str) -> str:
    """尽力修复截断的 JSON：补全未闭合的字符串引号与括号，去掉尾部残缺。

    适用场景：模型输出达到 max_tokens 被截断，JSON 停在键/值中间。
    策略：
      1) 逐字符扫描，统计未闭合括号，字符串内补引号
      2) 删除尾部悬空键（"key": 后无值）
      3) 若尾部键值对前缺逗号（截断时丢失），删除该键值对
      4) 补闭合括号
    """
    import re
    out = []
    in_str = False
    escape = False
    stack = []  # 括号嵌套栈：遇到 { 或 [ 时 push，用于精确补闭合
    for ch in t:
        if in_str:
            out.append(ch)
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
            out.append(ch)
        elif ch == "{":
            stack.append("{")
            out.append(ch)
        elif ch == "}":
            if stack and stack[-1] == "{":
                stack.pop()
            out.append(ch)
        elif ch == "[":
            stack.append("[")
            out.append(ch)
        elif ch == "]":
            if stack and stack[-1] == "[":
                stack.pop()
            out.append(ch)
        else:
            out.append(ch)
    # 停在字符串中间：补引号
    if in_str:
        out.append('"')
    s = "".join(out).rstrip()
    # 清理 JSON 不允许的尾逗号（模型常犯：对象/数组最后一个元素后带逗号）
    # 模式：逗号后紧跟 } 或 ]（含空白）
    s = re.sub(r",(\s*[}\]])", r"\1", s)
    # 删除尾部悬空键："key":（后无值）
    s = re.sub(r'"([^"\\]*(?:\\.[^"\\]*)*)"\s*:\s*$', "", s).rstrip()
    # 去尾逗号
    s = re.sub(r",\s*$", "", s)
    # 若尾部是完整键值对，且其前一个非空白字符不是 , { [（截断丢了逗号）→ 删该键值对
    m = re.search(r'"([^"\\]*(?:\\.[^"\\]*)*)"\s*:\s*"([^"\\]*(?:\\.[^"\\]*)*)"\s*$', s)
    if m:
        prefix = s[:m.start()].rstrip()
        if prefix and prefix[-1] not in ",{[":
            s = prefix.rstrip().rstrip(",")
    # 按嵌套栈逆序补闭合括号（先关内层，再关外层）
    for ch in reversed(stack):
        s += "}" if ch == "{" else "]"
    return s
