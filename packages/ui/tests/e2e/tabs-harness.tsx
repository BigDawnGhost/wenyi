import React, { useState } from "react";
import { createRoot } from "react-dom/client";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "../../src/components/ui/misc";
import "@wenyi/ui/styles.css";

function Harness() {
  const [value, setValue] = useState("overview");
  return (
    <div className="p-6">
      <Tabs value={value} onValueChange={setValue}>
        <TabsList aria-label="示例标签组">
          <TabsTrigger value="overview">概览</TabsTrigger>
          <TabsTrigger value="details">详情</TabsTrigger>
          <TabsTrigger value="settings">设置</TabsTrigger>
        </TabsList>
        <TabsContent value="overview">
          <p>概览面板内容</p>
        </TabsContent>
        <TabsContent value="details">
          <p>详情面板内容</p>
        </TabsContent>
        <TabsContent value="settings">
          <p>设置面板内容</p>
        </TabsContent>
      </Tabs>
    </div>
  );
}

export function mountTabsHarness() {
  createRoot(document.getElementById("root")!).render(<Harness />);
}
