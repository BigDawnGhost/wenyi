import { Pencil } from "lucide-react";
import { Button, type ButtonProps } from "./button";

type EditButtonProps = Pick<ButtonProps, "disabled" | "onClick"> & { label: string };

export function EditButton({ label, ...props }: EditButtonProps) {
  return (
    <Button type="button" variant="ghost" size="icon" aria-label={label} title={label} {...props}>
      <Pencil className="h-4 w-4" aria-hidden="true" />
    </Button>
  );
}
