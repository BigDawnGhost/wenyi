// Shared confirmation and cancellation contracts also run on Desktop.
import { registerTests } from "../../../../packages/ui/tests/e2e/project-deletion";
import { registerDesktopSetup } from "./shared-setup";

const apiOrigin = "http://127.0.0.1:19485";
registerDesktopSetup(apiOrigin);
registerTests({ apiOrigin });
