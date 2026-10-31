"use client";

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

/**
 * The workspace row at the top of the sidebar: the brand being shown, its main domain under it,
 * and a "Change" link when the customer has more than one brand.
 */
export function BrandPicker() {
  const { me, brand } = usePanel();
  const { update } = useUrlState();
  const label = brandLabel(me, brand);
  const only = me.brands.length === 1 ? me.brands[0] : null;
  const domain = (brand ?? only)?.primary_domain;
  const detail = domain ?? (brand || only ? me.customer.name : `${me.brands.length} brands`);

  return (
    <div className="flex items-baseline justify-between gap-2 border-b border-border pb-[18px]">
      <div className="min-w-0">
        <p className="truncate font-display text-[15px] font-semibold text-text">{label}</p>
        <p className="truncate text-[12.5px] text-text-3">{detail}</p>
      </div>
      {me.brands.length > 1 ? (
        <DropdownMenu>
          <DropdownMenuTrigger
            aria-label={`Change brand (showing ${label})`}
            className="flex-none rounded-xs text-[12.5px] font-semibold text-accent underline underline-offset-[3px] hover:text-accent-hover"
          >
            Change
          </DropdownMenuTrigger>
          <DropdownMenuContent align="start" className="min-w-48">
            <DropdownMenuLabel className="text-[12.5px] text-text-3">Brand</DropdownMenuLabel>
            <DropdownMenuRadioGroup
              value={brand?.id ?? ALL}
              onValueChange={(value) => update({ brand: value === ALL ? null : value, page: null })}
            >
              <DropdownMenuRadioItem value={ALL}>All brands</DropdownMenuRadioItem>
              {me.brands.map((item) => (
                <DropdownMenuRadioItem key={item.id} value={item.id}>
                  {item.name}
                </DropdownMenuRadioItem>
              ))}
            </DropdownMenuRadioGroup>
          </DropdownMenuContent>
        </DropdownMenu>
      ) : null}
    </div>
  );
}
