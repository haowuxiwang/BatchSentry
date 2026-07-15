import json, requests

with open("spike/page9.html", "r", encoding="utf-8") as f:
    page9_html = f.read()
with open("spike/page2.html", "r", encoding="utf-8") as f:
    page2_html = f.read()

API_KEY = "sk-evnpxdewgepantmkzlucfbokqgtukslyfktoqexquogrcxvz"
BASE_URL = "https://api.siliconflow.cn/v1"
MODEL = "deepseek-ai/DeepSeek-V4-Pro"

def call_llm(system_prompt, user_content, max_tokens=3000):
    payload = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content}
        ],
        "max_tokens": max_tokens,
        "temperature": 0.1,
    }
    resp = requests.post(
        f"{BASE_URL}/chat/completions",
        headers={"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"},
        json=payload, timeout=180
    )
    if resp.status_code == 200:
        return resp.json()["choices"][0]["message"]["content"]
    return f"ERROR {resp.status_code}: {resp.text[:300]}"

EXTRACTION_PROMPT = """你是一个制药行业GMP批生产记录数据提取专家。给你一页批生产记录的HTML表格(OCR识别产物)，请提取结构化信息并以JSON输出。

重点关注：
1. 页面标题(岗位/工序名称)
2. 文件编号、版本号、产品批号、生产日期
3. 所有工序步骤：步骤编号、操作描述、开始时间、结束时间、参数(名称/规格/实测值/单位/是否合格)、操作人、复核人、手写字段
4. 时间处理规则：只写HH:MM的从顶部生产日期推断完整日期；"X日X时X分"需转换；多年份出现标ANOMALY_MULTIPLE_YEARS；时间非单调递增标ANOMALY_TIME_REVERSAL
5. HTML结构噪声、LaTeX转换、置信度评定

严格输出JSON，不要markdown包裹：
{"page_info":{"title":"","file_code":"","version":"","batch_no":"","production_date":""},"steps":[{"step_no":"","operation":"","start_time":"","end_time":"","parameters":[{"name":"","spec_range":"","value":"","unit":"","in_spec":true}],"operator":"","reviewer":"","handwritten":[],"anomalies":[]}],"findings":[{"type":"","description":"","severity":""}],"time_anomalies":[],"overall_confidence":"high|medium|low"}"""

print("=" * 80)
print("TEST A: Page 9 (SP-1 resin absorption - clean expected)")
print("=" * 80)
result_a = call_llm(EXTRACTION_PROMPT, f"")
print(result_a[:4000])

print("
" + "=" * 80)
print("TEST B: Page 2 (multi-year mix expected)")
print("=" * 80)
result_b = call_llm(EXTRACTION_PROMPT, f"")
print(result_b[:4000])

with open("spike/result_a.txt", "w", encoding="utf-8") as f:
    f.write(result_a)
with open("spike/result_b.txt", "w", encoding="utf-8") as f:
    f.write(result_b)
print("
Saved to spike/")
