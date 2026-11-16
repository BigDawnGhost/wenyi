import { QueryClient } from "@tanstack/react-query";
import { configurePlatform } from "@wenyi/ui/platform";
import { desktopPlatform } from "../src/platform";
import { mountNotificationsHarness } from "../../../../packages/ui/tests/e2e/notifications-harness";

configurePlatform(desktopPlatform(new QueryClient()));
mountNotificationsHarness();
