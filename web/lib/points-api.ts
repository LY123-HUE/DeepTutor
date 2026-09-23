import { apiFetch, apiUrl } from "@/lib/api";

export interface PointsSummary {
  account: {
    total_points_earned: number;
    total_quota_granted: number;
    current_streak_day: number;
    last_checkin_date: string;
  };
  checked_in_today: boolean;
  next_cycle_day: number;
  next_reward_points: number;
  timezone: string;
}

export interface CheckinResult {
  reward: {
    cycle_day: number;
    points_awarded: number;
    quota_amount: number;
    grant_status: string;
    created_at: string;
  };
  already_checked_in: boolean;
}

export interface RewardHistory {
  items: Array<{
    id: number;
    event_type: string;
    cycle_day: number;
    points_awarded: number;
    quota_amount: number;
    grant_status: string;
    created_at: string;
  }>;
}

async function readJson<T>(response: Response): Promise<T> {
  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    const detail = typeof body?.detail === "string" ? body.detail : "积分服务请求失败";
    throw new Error(detail);
  }
  return body as T;
}

export async function getPointsSummary(): Promise<PointsSummary> {
  return readJson(await apiFetch(apiUrl("/api/points/summary"), { cache: "no-store" }));
}

export async function checkIn(): Promise<CheckinResult> {
  return readJson(await apiFetch(apiUrl("/api/points/checkin"), { method: "POST" }));
}

export async function getRewardHistory(limit = 50): Promise<RewardHistory> {
  return readJson(
    await apiFetch(apiUrl(`/api/points/rewards?limit=${Math.max(1, Math.min(limit, 100))}`), {
      cache: "no-store",
    }),
  );
}
