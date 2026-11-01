import { registerTests } from "../../../../packages/ui/tests/e2e/control-geometry";
import { registerDesktopSetup } from "./shared-setup";

const apiOrigin = "http://127.0.0.1:19481";
registerDesktopSetup(apiOrigin);
registerTests({ apiOrigin });
