import { apiFetch, apiUrl } from "@/lib/api";
import { invalidateClientCache, withClientCache } from "@/lib/client-cache";
import type { LLMSelection } from "@/features/chat/model/protocol";

const LLM_OPTIONS_CACHE_KEY = "llm-options:list";
const DEFAULT_LLM_OPTIONS_TIMEOUT_MS = 30_000;

export interface LLMOption extends LLMSelection {
  profile_name: string;
  model_name: string;
  model: string;
  provider: string;
  /** Human-readable provider name from the registry ("OpenRouter"). */
  provider_label?: string;
  /** Tokengine model_type (1=chat 2=image 3=video 4=rerank 5=embedding).
   *  Absent for providers that don't classify their models. */
  model_type?: number;
  context_window?: number;
  reasoning_effort?: string;
  supported_reasoning_efforts?: string[];
  is_active_default: boolean;
}

export interface LLMOptionsResponse {
  active: LLMSelection | null;
  options: LLMOption[];
}

/** Rerank (4) and embedding (5) models are RAG pipeline components, never
 *  conversation targets. Mirrors the backend `is_chat_model` filter so the
 *  chat picker stays clean even against an older backend / stale catalog
 *  that synced such models into the LLM profile. */
const NON_CHAT_MODEL_TYPES = new Set([4, 5]);

export function isChatLLMOption(option: {
  model?: string;
  model_type?: number;
}): boolean {
  if (typeof option.model_type === "number") {
    return !NON_CHAT_MODEL_TYPES.has(option.model_type);
  }
  // Untyped entries are treated as chat models (pure model_type judgment).
  return true;
}

export function llmSelectionKey(selection: LLMSelection | null | undefined) {
  if (!selection?.profile_id || !selection.model_id) return "";
  return `${selection.profile_id}:${selection.model_id}`;
}

export function sameLLMSelection(
  a: LLMSelection | null | undefined,
  b: LLMSelection | null | undefined,
) {
  return llmSelectionKey(a) === llmSelectionKey(b);
}

/** List the configured model profiles.
 *
 *  Cached so the many consumers that need the model list (composer, model
 *  picker, capability gate, partner forms) share one round-trip instead of
 *  each firing their own on mount. Editing a profile calls
 *  ``invalidateLLMOptionsCache``; pass ``force`` to bypass the cache. */
export async function listLLMOptions(options?: {
  force?: boolean;
  timeoutMs?: number;
}): Promise<LLMOptionsResponse> {
  return withClientCache<LLMOptionsResponse>(
    LLM_OPTIONS_CACHE_KEY,
    async () => {
      const controller = new AbortController();
      const timeout = setTimeout(
        () => controller.abort(),
        options?.timeoutMs ?? DEFAULT_LLM_OPTIONS_TIMEOUT_MS,
      );
      try {
        const response = await apiFetch(apiUrl("/api/settings/llm-options"), {
          cache: "no-store",
          signal: controller.signal,
        });
        if (!response.ok) {
          throw new Error(`Failed to load LLM options: ${response.status}`);
        }
        const data = (await response.json()) as LLMOptionsResponse;
        // Defense in depth: never offer embedding/rerank models in the
        // conversation picker, even if an older backend/catalog listed them.
        const options = (Array.isArray(data.options) ? data.options : []).filter(
          isChatLLMOption,
        );
        // A persisted/active selection pointing at a now-hidden model must
        // not survive as the default — fall back to "no active" so callers
        // re-select a real chat model.
        const offered = new Set(options.map((o) => llmSelectionKey(o)));
        const active =
          data.active && offered.has(llmSelectionKey(data.active))
            ? data.active
            : null;
        return { active, options };
      } finally {
        clearTimeout(timeout);
      }
    },
    { force: options?.force },
  );
}

export function invalidateLLMOptionsCache(): void {
  invalidateClientCache(LLM_OPTIONS_CACHE_KEY);
}
