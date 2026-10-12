// SPDX-FileCopyrightText: 2026 The Switchyard contributors
//
// SPDX-License-Identifier: Apache-2.0

import { describe, expect, it } from "vitest";
import { duration } from "@/features/jobs/Jobs";

describe("job duration", () => {
  it("formats seconds, minutes and open jobs", () => {
    const at = (s: number) => new Date(Date.parse("2026-10-11T10:00:00Z") + s * 1000).toISOString();
    expect(duration({ started_at: null, finished_at: null })).toBe("—");
    expect(duration({ started_at: at(0), finished_at: at(4) })).toBe("4 s");
    expect(duration({ started_at: at(0), finished_at: at(64) })).toBe("1 min 4 s");
    expect(duration({ started_at: at(0), finished_at: at(720) })).toBe("12 min");
    expect(duration({ started_at: at(0), finished_at: null }, Date.parse(at(30)))).toBe("30 s");
  });
});
