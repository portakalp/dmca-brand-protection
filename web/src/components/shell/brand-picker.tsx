"use client";

import { ChevronDown } from "lucide-react";

import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuLabel,
  DropdownMenuRadioGroup,
  DropdownMenuRadioItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { useUrlState } from "@/lib/url";

import { brandLabel, usePanel } from "./panel";

const ALL = "all";

export function BrandPicker() {
  const { me, brand } = usePanel();
  const { update } = useUrlState();
  const label = brandLabel(me, brand);
  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button
          variant="control"
          size="control"
          className="pr-3 pl-2.5"
          aria-label={`Brand: ${label}`}
        >
          <span
            aria-hidden="true"
            className="inline-flex size-5 items-center justify-center rounded-md bg-[var(--brand-chip-bg)] text-[11px] font-bold text-[var(--brand-chip-fg)]"
          >
            {label === "All brands" ? "∗" : label.slice(0, 1).toUpperCase()}
          </span>
          {label}
          <ChevronDown aria-hidden="true" className="text-text-tertiary" />
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" className="min-w-48">
        <DropdownMenuLabel>Brand</DropdownMenuLabel>
        <DropdownMenuRadioGroup
          value={brand?.id ?? ALL}
          onValueChange={(value) => update({ brand: value === ALL ? null : value, page: null })}
        >
          {me.brands.length > 1 ? (
            <DropdownMenuRadioItem value={ALL}>All brands</DropdownMenuRadioItem>
          ) : null}
          {me.brands.map((item) => (
            <DropdownMenuRadioItem key={item.id} value={me.brands.length > 1 ? item.id : ALL}>
              {item.name}
            </DropdownMenuRadioItem>
          ))}
        </DropdownMenuRadioGroup>
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
