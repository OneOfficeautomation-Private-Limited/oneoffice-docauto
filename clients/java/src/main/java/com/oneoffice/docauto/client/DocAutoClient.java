package com.oneoffice.docauto.client;

import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.net.URI;
import java.net.URLEncoder;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.time.Duration;
import java.util.UUID;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/**
 * Zero-dependency Java client for the Document Automation component.
 *
 * <p>This is the thin layer the main RPA tool (bot runner / action packages) uses to call the
 * component over REST. Methods return the raw JSON string so the host can parse it with whatever
 * JSON library it already uses (Jackson, Gson, ...).
 *
 * <pre>
 * DocAutoClient c = new DocAutoClient("http://localhost:8085", System.getenv("DOCAUTO_API_TOKEN"));
 * String job = c.uploadDocument(Path.of("invoice.pdf"), "{\"schemaId\":\"supplier-invoice\"}");
 * String docId = DocAutoClient.jsonString(job, "documentId");
 * c.waitForJob(DocAutoClient.jsonString(job, "jobId"), Duration.ofMinutes(2));
 * String fields = c.getFields(docId);
 * </pre>
 */
public class DocAutoClient {
    private static final String API = "/api/docs/v1";
    private final String baseUrl;
    private final String token;
    private final HttpClient http;

    public DocAutoClient(String baseUrl, String token) {
        this.baseUrl = baseUrl.replaceAll("/+$", "");
        this.token = token;
        this.http = HttpClient.newBuilder().connectTimeout(Duration.ofSeconds(10)).build();
    }

    // ------------------------------------------------------------------ actions

    /** Upload documents: starts async processing. Returns {"jobId","documentId","status"}. */
    public String uploadDocument(Path file, String optionsJson) throws IOException, InterruptedException {
        return uploadDocument(file, optionsJson, false);
    }

    /** Upload and optionally process synchronously (wait=true). */
    public String uploadDocument(Path file, String optionsJson, boolean wait) throws IOException, InterruptedException {
        Multipart mp = new Multipart();
        mp.file("file", file.getFileName().toString(), Files.readAllBytes(file));
        mp.field("options", optionsJson == null ? "{}" : optionsJson);
        return send(mp.request(uri(API + "/documents?wait=" + wait)), 202);
    }

    /** Get extraction results status. */
    public String getJob(String jobId) throws IOException, InterruptedException {
        return send(get(API + "/jobs/" + jobId), 200);
    }

    /** Poll until the job is completed / review / failed. Returns the final job JSON. */
    public String waitForJob(String jobId, Duration timeout) throws IOException, InterruptedException {
        long deadline = System.currentTimeMillis() + timeout.toMillis();
        while (true) {
            String job = getJob(jobId);
            String status = jsonString(job, "status");
            if ("completed".equals(status) || "review".equals(status) || "failed".equals(status)) {
                return job;
            }
            if (System.currentTimeMillis() > deadline) {
                throw new IOException("timeout waiting for job " + jobId + " (status " + status + ")");
            }
            Thread.sleep(1000);
        }
    }

    /** Extract data: flat {fields:{name:value}, confidence:{...}, reviewRequired}. */
    public String getFields(String documentId) throws IOException, InterruptedException {
        return send(get(API + "/documents/" + documentId + "/fields"), 200);
    }

    /** Full normalized DocumentResult JSON (text, pages, lines, tables, fields, provenance). */
    public String getResult(String documentId) throws IOException, InterruptedException {
        return send(get(API + "/documents/" + documentId + "/result"), 200);
    }

    /** Download results as csv | xlsx | json. */
    public byte[] downloadResults(String documentId, String format) throws IOException, InterruptedException {
        return sendBytes(get(API + "/documents/" + documentId + "/export?format=" + enc(format)));
    }

    /** Send to validation: the document is already queued when review is required; list the queue. */
    public String listPendingValidation() throws IOException, InterruptedException {
        return send(get(API + "/reviews?status=pending"), 200);
    }

    /** Complete a validation programmatically (e.g. corrections from another system). */
    public String completeValidation(String documentId, String reviewer, String fieldsJson)
            throws IOException, InterruptedException {
        String body = "{\"reviewer\":\"" + reviewer.replace("\"", "") + "\",\"fields\":"
                + (fieldsJson == null ? "{}" : fieldsJson) + "}";
        HttpRequest req = auth(HttpRequest.newBuilder(uri(API + "/reviews/" + documentId + "/complete")))
                .header("Content-Type", "application/json")
                .POST(HttpRequest.BodyPublishers.ofString(body)).build();
        return send(req, 200);
    }

    /** Screen OCR (synchronous) for Citrix/RDP/legacy screens. */
    public String screenOcr(byte[] png, String provider, String languages) throws IOException, InterruptedException {
        Multipart mp = new Multipart();
        mp.file("file", "screen.png", png);
        mp.field("provider", provider == null ? "auto" : provider);
        mp.field("languages", languages == null ? "en" : languages);
        return send(mp.request(uri(API + "/ocr/screen")), 200);
    }

    /** Make searchable PDF. */
    public byte[] searchablePdf(Path file, String languages) throws IOException, InterruptedException {
        Multipart mp = new Multipart();
        mp.file("file", file.getFileName().toString(), Files.readAllBytes(file));
        mp.field("languages", languages == null ? "en" : languages);
        return sendBytes(mp.request(uri(API + "/pdf/searchable")));
    }

    /** Engines, option packs and policy status (for the designer's provider dropdown). */
    public String listProviders() throws IOException, InterruptedException {
        return send(get(API + "/providers"), 200);
    }

    public String saveSchema(String schemaJson) throws IOException, InterruptedException {
        HttpRequest req = auth(HttpRequest.newBuilder(uri(API + "/schemas")))
                .header("Content-Type", "application/json")
                .POST(HttpRequest.BodyPublishers.ofString(schemaJson)).build();
        return send(req, 200);
    }

    // ------------------------------------------------------------------ Oneoffice DocFlow

    /** List Doc Flows (including the Smart Inbox) with status and statistics. */
    public String listFlows() throws IOException, InterruptedException {
        return send(get(API + "/df/flows"), 200);
    }

    /**
     * Send document to flow: a LIVE flow id, or "smart-inbox" to let DocFlow recognise and split it.
     * Returns {"documents":[{"documentId","jobId","status","flowId","doctypeId"}]}.
     */
    public String sendToFlow(String flowId, Path file) throws IOException, InterruptedException {
        Multipart mp = new Multipart();
        mp.file("files", file.getFileName().toString(), Files.readAllBytes(file));
        return send(mp.request(uri(API + "/df/flows/" + flowId + "/documents?source=bot&requireLive=true")), 202);
    }

    /**
     * Get document result: status (queued|processing|success|validation|validated|invalid|failed|split),
     * fields, confidence, rule flags and - for a split batch - its child documents.
     */
    public String getDocumentResult(String documentId) throws IOException, InterruptedException {
        return send(get(API + "/df/documents/" + documentId), 200);
    }

    /**
     * Wait for document: polls until finished. With waitForReview=true it also waits while a person reviews it.
     * For a split batch it returns when the batch is split; read "children" and wait for each child.
     */
    public String waitForDocument(String documentId, Duration timeout, boolean waitForReview)
            throws IOException, InterruptedException {
        long deadline = System.currentTimeMillis() + timeout.toMillis();
        while (true) {
            String doc = getDocumentResult(documentId);
            String status = jsonString(doc, "status");
            boolean done = "success".equals(status) || "validated".equals(status) || "invalid".equals(status)
                    || "failed".equals(status) || "split".equals(status)
                    || ("validation".equals(status) && !waitForReview);
            if (done) {
                return doc;
            }
            if (System.currentTimeMillis() > deadline) {
                throw new IOException("timeout waiting for document " + documentId + " (status " + status + ")");
            }
            Thread.sleep(2000);
        }
    }

    /** Export flow results (format xlsx|csv): successful and reviewed documents. */
    public byte[] exportFlow(String flowId, String format) throws IOException, InterruptedException {
        return sendBytes(get(API + "/df/flows/" + flowId + "/export?format=" + enc(format)));
    }

    // ------------------------------------------------------------------ helpers

    /** Minimal extractor for a top-level string value, to avoid forcing a JSON dependency. */
    public static String jsonString(String json, String key) {
        Matcher m = Pattern.compile("\"" + Pattern.quote(key) + "\"\\s*:\\s*\"([^\"]*)\"").matcher(json);
        return m.find() ? m.group(1) : null;
    }

    private URI uri(String path) {
        return URI.create(baseUrl + path);
    }

    private static String enc(String s) {
        return URLEncoder.encode(s, StandardCharsets.UTF_8);
    }

    private HttpRequest.Builder auth(HttpRequest.Builder b) {
        if (token != null && !token.isEmpty()) {
            b.header("Authorization", "Bearer " + token);
        }
        return b.timeout(Duration.ofMinutes(5));
    }

    private HttpRequest get(String path) {
        return auth(HttpRequest.newBuilder(uri(path))).GET().build();
    }

    private String send(HttpRequest req, int expected) throws IOException, InterruptedException {
        HttpResponse<String> r = http.send(req, HttpResponse.BodyHandlers.ofString(StandardCharsets.UTF_8));
        if (r.statusCode() != expected && r.statusCode() / 100 != 2) {
            throw new DocAutoException(r.statusCode(), r.body());
        }
        return r.body();
    }

    private byte[] sendBytes(HttpRequest req) throws IOException, InterruptedException {
        HttpResponse<byte[]> r = http.send(req, HttpResponse.BodyHandlers.ofByteArray());
        if (r.statusCode() / 100 != 2) {
            throw new DocAutoException(r.statusCode(), new String(r.body(), StandardCharsets.UTF_8));
        }
        return r.body();
    }

    /** Error raised for non-2xx responses; body carries {code,message,providerId,retryable}. */
    public static class DocAutoException extends IOException {
        public final int status;
        public final String body;

        public DocAutoException(int status, String body) {
            super("HTTP " + status + ": " + body);
            this.status = status;
            this.body = body;
        }

        public String code() {
            return jsonString(body, "code");
        }
    }

    private class Multipart {
        private final String boundary = "----docauto" + UUID.randomUUID();
        private final ByteArrayOutputStream out = new ByteArrayOutputStream();

        void field(String name, String value) throws IOException {
            out.write(("--" + boundary + "\r\nContent-Disposition: form-data; name=\"" + name + "\"\r\n\r\n")
                    .getBytes(StandardCharsets.UTF_8));
            out.write(value.getBytes(StandardCharsets.UTF_8));
            out.write("\r\n".getBytes(StandardCharsets.UTF_8));
        }

        void file(String name, String filename, byte[] data) throws IOException {
            out.write(("--" + boundary + "\r\nContent-Disposition: form-data; name=\"" + name + "\"; filename=\""
                    + filename.replace("\"", "") + "\"\r\nContent-Type: application/octet-stream\r\n\r\n")
                    .getBytes(StandardCharsets.UTF_8));
            out.write(data);
            out.write("\r\n".getBytes(StandardCharsets.UTF_8));
        }

        HttpRequest request(URI target) {
            byte[] body = (new String(out.toByteArray(), StandardCharsets.ISO_8859_1) + "--" + boundary + "--\r\n")
                    .getBytes(StandardCharsets.ISO_8859_1);
            return auth(HttpRequest.newBuilder(target))
                    .header("Content-Type", "multipart/form-data; boundary=" + boundary)
                    .POST(HttpRequest.BodyPublishers.ofByteArray(body)).build();
        }
    }
}
