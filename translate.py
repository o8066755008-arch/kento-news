"""Claude API を使う処理（海外ニュースの翻訳、会員数の読み取り）。

環境変数 ANTHROPIC_API_KEY が必要。未設定なら何もせず None を返し、
次回以降の実行でまとめて処理する。
"""
import json
import os
import sys

MODEL = "claude-opus-5-5"
# 料金（1M トークンあたりのドル）と円換算。実行ごとの使用量と費用の目安を記録に出す
PRICE_IN, PRICE_OUT, YEN = 4.00, 20.00, 150
_usage = {"in": 0, "out": 0, "calls": 0}


def _report_usage() -> None:
    if _usage["calls"]:
        usd = _usage["in"] / 1e6 * PRICE_IN + _usage["out"] / 1e6 * PRICE_OUT
        print(f"[AI 使用量] {_usage['calls']} 回 / 入力 {_usage['in']:,} / 出力 {_usage['out']:,} トークン"
              f"（約 {usd * YEN:.1f} 円）", flush=True)


import atexit  # noqa: E402
atexit.register(_report_usage)

TRANSLATE_SYSTEM = (
    "あなたは芸能ニュースの翻訳者です。渡された見出しを自然な日本語に翻訳してください。"
    "人名・グループ名は日本で一般的な表記にします（例: 나카지마 켄토 / Kento Nakajima → 中島健人）。"
    "入力と同じ順番・同じ件数で返してください。"
)

TRANSLATE_SCHEMA = {
    "type": "object",
    "properties": {"translations": {"type": "array", "items": {"type": "string"}}},
    "required": ["translations"],
    "additionalProperties": False,
}


def ask_json(system: str, content: str, schema: dict) -> dict | None:
    """Claude に質問し、schema どおりの JSON を受け取る。失敗したら None。"""
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ANTHROPIC_API_KEY が未設定のため AI 処理をスキップします", file=sys.stderr)
        return None
    import anthropic

    client = anthropic.Anthropic()
    try:
        response = client.beta.messages.create(
            model=MODEL,
            max_tokens=16000,
            system=system,
            messages=[{"role": "user", "content": content}],
            output_config={
                "effort": "low",
                "format": {"type": "json_schema", "schema": schema},
            },
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        )
    except anthropic.APIError as e:
        print(f"AI 処理に失敗しました: {e}", file=sys.stderr)
        return None
    _usage["calls"] += 1
    _usage["in"] += response.usage.input_tokens
    _usage["out"] += response.usage.output_tokens
    if response.stop_reason != "end_turn":
        print(f"AI 処理が完了しませんでした: {response.stop_reason}", file=sys.stderr)
        return None
    text = next(b.text for b in response.content if b.type == "text")
    return json.loads(text)


def to_japanese(titles: list[str]) -> list[str] | None:
    numbered = "\n".join(f"{i + 1}. {t}" for i, t in enumerate(titles))
    result = ask_json(TRANSLATE_SYSTEM, numbered, TRANSLATE_SCHEMA)
    if result is None:
        return None
    translations = result["translations"]
    if len(translations) != len(titles):
        print("翻訳の件数が一致しないため保存しません", file=sys.stderr)
        return None
    return translations
