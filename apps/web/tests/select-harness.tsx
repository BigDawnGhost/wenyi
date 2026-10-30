import React, { useState } from "react";
import { createRoot } from "react-dom/client";
import { Select, SelectItem } from "@wenyi/ui/components/ui/select";
import { Label } from "@wenyi/ui/components/ui/form";
import "@wenyi/ui/index.css";

const longLabel = "很长的中文选项用于检查小窗口内的换行与边缘碰撞".repeat(6);

function Harness() {
  const [value, setValue] = useState("");
  return (
    <form className="ml-auto mt-24 w-64 max-w-full p-2" id="example">
      <button type="button" onClick={() => setValue("alpha")}>Set controlled value</button>
      <Label htmlFor="controlled">受控语言</Label>
      <Select id="controlled" name="language" value={value} onValueChange={setValue}>
        <SelectItem value="">全部语言</SelectItem>
        <SelectItem value="disabled" disabled>不可用</SelectItem>
        <SelectItem value="alpha">Alpha</SelectItem>
        <SelectItem value="中文">中文</SelectItem>
        <SelectItem value="long">{longLabel}</SelectItem>
        {Array.from({ length: 30 }, (_, index) =>
          <SelectItem key={index} value={`item-${index}`}>项目 {index}</SelectItem>,
        )}
      </Select>
      <button type="button">After select</button>
      <Select aria-label="Default value" name="default" defaultValue="value:">
        <SelectItem value="">None</SelectItem>
        <SelectItem value="value:">Encoded-looking value</SelectItem>
        <SelectItem value="other">Other</SelectItem>
      </Select>
      <Select aria-label="Disabled select" name="disabled" defaultValue="a" disabled>
        <SelectItem value="a">Disabled</SelectItem>
      </Select>
      <fieldset disabled>
        <Select aria-label="Disabled fieldset" name="fieldset" defaultValue="a">
          <SelectItem value="a">Disabled by fieldset</SelectItem>
        </Select>
      </fieldset>
    </form>
  );
}

createRoot(document.getElementById("root")!).render(<Harness />);
