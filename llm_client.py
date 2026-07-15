import json
from openai import OpenAI
from config import LLM_CONFIGS, LLM_PROVIDER
from llm_prompts import SYSTEM_PROMPT, PAGE_ANALYSIS_PROMPT


class LLMClient:
    """Abstract LLM client that supports multiple Chinese multimodal models."""

    def __init__(self, provider: str = None):
        self.provider = provider or LLM_PROVIDER
        cfg = LLM_CONFIGS[self.provider]
        self.client = OpenAI(api_key=cfg["api_key"], base_url=cfg["base_url"])
        self.model = cfg["model"]

    def analyze_page(self, image_base64: str, page_num: int) -> dict:
        """Send a page image to LLM and return structured analysis."""
        prompt = PAGE_ANALYSIS_PROMPT.replace("{page_num}", str(page_num))

        response = self.client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:image/png;base64,{image_base64}",
                            },
                        },
                    ],
                },
            ],
            temperature=0.1,
        )

        content = response.choices[0].message.content
        return self._parse_json(content)

    def generate_report(self, page_results: list) -> str:
        """Generate final compliance report from all page results."""
        from llm_prompts import REPORT_GENERATION_PROMPT

        results_text = json.dumps(page_results, ensure_ascii=False, indent=2)
        prompt = REPORT_GENERATION_PROMPT.replace("{page_results}", results_text)

        response = self.client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": "你是制药行业 GMP 合规报告撰写专家。"},
                {"role": "user", "content": prompt},
            ],
            temperature=0.3,
        )

        return response.choices[0].message.content

    def _parse_json(self, text: str) -> dict:
        """Extract JSON from LLM response text."""
        # Try to find JSON block in markdown code fence
        if "```json" in text:
            start = text.index("```json") + 7
            end = text.index("```", start)
            text = text[start:end].strip()
        elif "```" in text:
            start = text.index("```") + 3
            end = text.index("```", start)
            text = text[start:end].strip()

        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return {"page_number": 0, "findings": [], "summary": text, "parse_error": True}
