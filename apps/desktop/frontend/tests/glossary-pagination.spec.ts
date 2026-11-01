// Exercise the shared glossary UI through the Desktop host and transport.
import { registerTests } from "../../../../packages/ui/tests/e2e/glossary-pagination";
import { registerDesktopSetup } from "./shared-setup";

const apiOrigin = "http://127.0.0.1:19481";
registerDesktopSetup(apiOrigin);
registerTests({ apiOrigin });
