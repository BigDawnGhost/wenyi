import { registerTests } from "../../../../packages/ui/tests/e2e/glossary-import";
import { registerDesktopSetup } from "./shared-setup";

const origin = "http://127.0.0.1:19481";
registerDesktopSetup(origin);
registerTests(origin);
