import { configurePlatform } from "@wenyi/ui/platform";
import { webPlatform } from "../src/platform";
import { mountNotificationsHarness } from "../../../packages/ui/tests/e2e/notifications-harness";

configurePlatform(webPlatform);
mountNotificationsHarness();
