import axios from "axios";

const api = axios.create({
  baseURL: "https://paperpilot-ai-backend.onrender.com",
});

export default api;
