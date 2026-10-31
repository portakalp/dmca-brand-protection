"use client";

import { createContext, useContext, type ReactNode } from "react";

import type { Schemas } from "@/lib/api/client";

export type Panel = {
  me: Schemas["Me"];
  timeZone: string;
  /** The brand in the URL when it is one of the customer's; null means all brands. */
  brand: Schemas["BrandOut"] | null;
};

const PanelContext = createContext<Panel | null>(null);

export function PanelProvider({ value, children }: { value: Panel; children: ReactNode }) {
  return <PanelContext.Provider value={value}>{children}</PanelContext.Provider>;
}

export function usePanel(): Panel {
  const panel = useContext(PanelContext);
  if (!panel) {
    throw new Error("usePanel needs the app shell");
  }
  return panel;
}

export function pickBrand(me: Schemas["Me"], id: string | null): Schemas["BrandOut"] | null {
  return me.brands.find((brand) => brand.id === id) ?? null;
}

/** "Mintera Bank" with one brand or one chosen, "All brands" otherwise. */
export function brandLabel(me: Schemas["Me"], brand: Schemas["BrandOut"] | null): string {
  if (brand) return brand.name;
  if (me.brands.length === 1) return me.brands[0].name;
  return "All brands";
}
