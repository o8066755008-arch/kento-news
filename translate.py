"""海外ニュースの見出しを Claude API で日本語に翻訳する。

環境変数 ANTHROPIC_API_KEY が必要。未設定なら翻訳をスキップし、
次回以降の実行でまとめて翻訳する。
"""
import json
import os
import sys

MODEL = "claude-opus-5-5"

SYSTEM = (
    "あなたは芸能ニュースの翻訳者です。渡された見出しを自然な日本語に翻訳してください。"
    "人名・グループ名は日本で一般的な表記にします（例: 나카지마 켄토 / Kento Nakajima → 中島健人）。"
    "入力と同じ順番・同じ件数で返してください。"
)

SCHEMA = {
    "type": "object",
    "properties": {"translations": {"type": "array", "items": {"type": "string"}}},
    "required": ["translations"],
    "additionalProperties": False,
}


def to_japanese(titles: list[str]) -> list[str] | None:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ANTHROPIC_API_KEY が未設定のため翻訳をスキップします", file=sys.stderr)
        return None
    import anthropic

    client = anthropic.Anthropic()
    numbered = "\n".join(f"{i + 1}. {t}" for i, t in enumerate(titles))
    try:
        response = client.beta.messages.create(
            model=MODEL,
            max_tokens=16000,
            system=SYSTEM,
            messages=[{"role": "user", "content": numbered}],
            output_config={
                "effort": "low",
                "format": {"type": "json_schema", "schema": SCHEMA},
            },
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        )
    except anthropic.APIError as e:
        print(f"翻訳に失敗しました: {e}", file=sys.stderr)
        return None
    if response.stop_reason != "end_turn":
        print(f"翻訳が完了しませんでした: {response.stop_reason}", file=sys.stderr)
        return None
    text = next(b.text for b in response.content if b.type == "text")
    translations = json.loads(text)["translations"]
    if len(translations) != len(titles):
        print("翻訳の件数が一致しないため保存しません", file=sys.stderr)
        return None
    return translations
