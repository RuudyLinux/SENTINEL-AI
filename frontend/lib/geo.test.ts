import { describe, expect, it } from "vitest";
import { hasLocation } from "./geo";

describe("hasLocation", () => {
  it("accepts a real position", () => {
    expect(hasLocation({ lat: 23.03, lng: 72.58 })).toBe(true);
  });

  it("treats the backend's 0,0 default as unknown", () => {
    expect(hasLocation({ lat: 0, lng: 0 })).toBe(false);
  });

  it("rejects missing, non-finite and out-of-range values", () => {
    expect(hasLocation(null)).toBe(false);
    expect(hasLocation({ lat: null, lng: 72 })).toBe(false);
    expect(hasLocation({ lat: NaN, lng: 72 })).toBe(false);
    expect(hasLocation({ lat: 91, lng: 72 })).toBe(false);
    expect(hasLocation({ lat: 23, lng: 181 })).toBe(false);
  });

  it("keeps a real position on one axis being zero", () => {
    expect(hasLocation({ lat: 0, lng: 72.58 })).toBe(true);
  });
});
