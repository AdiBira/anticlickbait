"""
Thumbnail evaluator using OpenAI Vision API.
Analyzes YouTube thumbnails for clickbait characteristics.
"""

import json
import time
import base64
from pathlib import Path
from openai import OpenAI, APIError, RateLimitError, APIConnectionError

from config import (
    OPENAI_API_KEY,
    OPENAI_MODEL,
    OPENAI_MAX_RETRIES,
    OPENAI_RETRY_DELAY_SECONDS,
    THUMBNAIL_PROMPT_PATH,
)


def _load_prompt() -> str:
    """Load thumbnail analysis prompt from file."""
    if THUMBNAIL_PROMPT_PATH.exists():
        return THUMBNAIL_PROMPT_PATH.read_text(encoding="utf-8")
    return "Analyze this YouTube thumbnail for clickbait. Return JSON with thumbnail_sensationalism_score (0-10), thumbnail_content_match (0-10), thumbnail_deception_flag (bool), thumbnail_explanation (str)."


def _encode_image(image_path: Path) -> str:
    """Encode image to base64."""
    with open(image_path, "rb") as f:
        return base64.b64encode(f.read()).decode("utf-8")


def _create_fallback(video_id: str, error: str) -> dict:
    """Create fallback response on failure."""
    return {
        "video_id": video_id,
        "thumbnail_sensationalism_score": -1.0,
        "thumbnail_content_match": -1.0,
        "thumbnail_deception_flag": None,
        "thumbnail_visual_elements": {},
        "thumbnail_explanation": f"Evaluation failed: {error}",
        "evaluation_success": False,
        "error": error,
    }


def _parse_response(text: str, video_id: str) -> dict:
    """Parse JSON from LLM response."""
    text = text.strip()
    if text.startswith("```json"):
        text = text[7:]
    elif text.startswith("```"):
        text = text[3:]
    if text.endswith("```"):
        text = text[:-3]
    text = text.strip()

    result = json.loads(text)
    result["video_id"] = video_id
    result["evaluation_success"] = True
    result["error"] = None
    return result


def evaluate_thumbnail(
    video_id: str,
    thumbnail_path: Path,
    video_title: str = None,
    max_retries: int = None,
    retry_delay: float = None,
) -> dict:
    """
    Evaluate a thumbnail for clickbait characteristics.

    Args:
        video_id: YouTube video ID
        thumbnail_path: Path to thumbnail image
        video_title: Optional title for context
        max_retries: Max retry attempts
        retry_delay: Base delay between retries

    Returns:
        dict with thumbnail metrics or fallback on failure
    """
    if max_retries is None:
        max_retries = OPENAI_MAX_RETRIES
    if retry_delay is None:
        retry_delay = OPENAI_RETRY_DELAY_SECONDS

    if not OPENAI_API_KEY:
        return _create_fallback(video_id, "OPENAI_API_KEY not set")

    if not thumbnail_path.exists():
        return _create_fallback(video_id, f"Thumbnail not found: {thumbnail_path}")

    client = OpenAI(api_key=OPENAI_API_KEY)
    system_prompt = _load_prompt()

    # Build user message with image
    image_b64 = _encode_image(thumbnail_path)
    user_content = [
        {
            "type": "image_url",
            "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"},
        },
    ]
    if video_title:
        user_content.append({"type": "text", "text": f"Video title: {video_title}"})

    last_error = None

    for attempt in range(max_retries):
        try:
            response = client.chat.completions.create(
                model=OPENAI_MODEL,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_content},
                ],
                temperature=0.0,
                max_tokens=500,
            )

            result = _parse_response(response.choices[0].message.content, video_id)
            return result

        except (RateLimitError, APIConnectionError, APIError) as e:
            last_error = str(e)
            wait = retry_delay * (2 ** attempt)
            print(f"API error, waiting {wait}s before retry {attempt + 1}/{max_retries}")
            time.sleep(wait)

        except json.JSONDecodeError as e:
            last_error = f"JSON parse error: {e}"
            wait = retry_delay * (2 ** attempt)
            print(f"Parse error, retrying...")
            time.sleep(wait)

        except Exception as e:
            last_error = str(e)
            break

    return _create_fallback(video_id, f"All retries failed: {last_error}")


def compute_thumbnail_score(result: dict) -> float | None:
    """
    Compute thumbnail score from evaluation.

    Formula:
        content_norm = content_match * 10 (0-100)
        sensational_norm = 100 - (sensationalism * 10) (inverted)
        deception_penalty = -40 if deception_flag else 0

        score = 0.5 * content_norm + 0.5 * sensational_norm + deception_penalty
    """
    if not result.get("evaluation_success"):
        return None

    content = result.get("thumbnail_content_match", 0)
    sensational = result.get("thumbnail_sensationalism_score", 0)
    deception = result.get("thumbnail_deception_flag", False)

    content_norm = content * 10
    sensational_norm = 100 - (sensational * 10)
    penalty = -40 if deception else 0

    return 0.5 * content_norm + 0.5 * sensational_norm + penalty
