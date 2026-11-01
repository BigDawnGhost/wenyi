import { registerTests } from "../../../../packages/ui/tests/e2e/sidebar";
import { registerDesktopSetup } from "./shared-setup";

const apiOrigin = "http://127.0.0.1:19485";
registerDesktopSetup(apiOrigin);
registerTests({ apiOrigin });
