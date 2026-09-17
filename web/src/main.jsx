import {createRoot} from "react-dom/client";

import AuthGate from "./AuthGate.jsx";
import "./styles.css";

createRoot(document.querySelector("#root")).render(<AuthGate />);
