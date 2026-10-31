import * as React from "react";
import { cva, type VariantProps } from "class-variance-authority";
import { cn } from "cn";
import { Slot } from "radix-ui";

// "Plain sight" buttons: bordered or filled so they read as interactive, 4px radius, 1.5px icon
// strokes, the global 2px focus outline (globals.css). No shadows.
const buttonVariants = cva(
  "group/button inline-flex shrink-0 items-center justify-center gap-2 rounded-sm border border-transparent bg-clip-padding text-[13.5px] leading-none font-semibold whitespace-nowrap no-underline transition-colors select-none hover:no-underline disabled:pointer-events-none disabled:opacity-55 aria-disabled:opacity-55 aria-invalid:border-destructive [&_svg]:pointer-events-none [&_svg]:shrink-0 [&_svg]:[stroke-width:1.5] [&_svg:not([class*='size-'])]:size-4",
  {
    variants: {
      variant: {
        // Primary: the one main action of a view.
        default:
          "border-accent bg-accent text-on-accent hover:border-accent-hover hover:bg-accent-hover hover:text-on-accent",
        // Secondary actions: bordered on the surface.
        outline:
          "border-border-strong bg-surface text-text hover:bg-sunken hover:text-text aria-expanded:bg-sunken",
        secondary:
          "border-border-strong bg-surface text-text hover:bg-sunken hover:text-text aria-expanded:bg-sunken",
        ghost:
          "text-text-2 hover:bg-sunken hover:text-text aria-expanded:bg-sunken aria-expanded:text-text",
        destructive:
          "border-st-critical bg-surface text-st-critical hover:bg-sunken hover:text-st-critical",
        link: "h-auto! border-0 px-0! text-accent underline underline-offset-[3px] decoration-1 hover:text-accent-hover hover:underline",
        // The old toolbar name; same as outline.
        control:
          "border-border-strong bg-surface text-text hover:bg-sunken hover:text-text aria-expanded:bg-sunken",
      },
      size: {
        default: "h-9 px-3.5",
        xs: "h-6 gap-1 px-2 text-xs [&_svg:not([class*='size-'])]:size-3",
        sm: "h-[30px] gap-1.5 px-2.5 text-[13px] [&_svg:not([class*='size-'])]:size-3.5",
        lg: "h-10 px-4 text-[14px]",
        icon: "size-9",
        "icon-xs": "size-6 [&_svg:not([class*='size-'])]:size-3.5",
        "icon-sm": "size-[30px]",
        "icon-lg": "size-10",
        // The old toolbar sizes.
        control: "h-9 px-3.5",
        "icon-control": "size-9",
        xl: "h-10 px-4 text-[14px]",
      },
    },
    defaultVariants: {
      variant: "default",
      size: "default",
    },
  },
);

function Button({
  className,
  variant = "default",
  size = "default",
  asChild = false,
  ...props
}: React.ComponentProps<"button"> &
  VariantProps<typeof buttonVariants> & {
    asChild?: boolean;
  }) {
  const Comp = asChild ? Slot.Root : "button";

  return (
    <Comp
      data-slot="button"
      data-variant={variant}
      data-size={size}
      className={cn(buttonVariants({ variant, size, className }))}
      {...props}
    />
  );
}

export { Button, buttonVariants };
