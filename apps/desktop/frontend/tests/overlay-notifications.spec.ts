// Shared UI contracts run against the Desktop platform entry as well.
import { registerTests } from "../../../../packages/ui/tests/e2e/overlay-notifications";
import { registerDesktopSetup } from "./shared-setup";

const apiOrigin = "http://127.0.0.1:19485";
registerDesktopSetup(apiOrigin);
registerTests({ apiOrigin });
