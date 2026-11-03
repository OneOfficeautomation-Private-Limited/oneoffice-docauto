package com.oneoffice.docauto.client;

import java.nio.file.Files;
import java.nio.file.Path;
import java.time.Duration;

/**
 * End-to-end example mirroring a bot: upload -> wait -> read fields -> (review) -> export.
 * Usage: java -cp out com.oneoffice.docauto.client.Example http://localhost:8085 samples/invoice_scan.png
 */
public class Example {
    public static void main(String[] args) throws Exception {
        String base = args.length > 0 ? args[0] : "http://localhost:8085";
        Path file = Path.of(args.length > 1 ? args[1] : "samples/invoice_scan.png");
        DocAutoClient client = new DocAutoClient(base, System.getenv("DOCAUTO_API_TOKEN"));

        Path schema = Path.of("samples/invoice_schema.json");
        if (Files.exists(schema)) {
            client.saveSchema(Files.readString(schema));
        }
        String job = client.uploadDocument(file,
                "{\"schemaId\":\"supplier-invoice\",\"languages\":[\"en\",\"ar\"],\"classifyLabels\":[\"invoice\",\"contract\"]}");
        String jobId = DocAutoClient.jsonString(job, "jobId");
        String docId = DocAutoClient.jsonString(job, "documentId");
        System.out.println("submitted job " + jobId + " document " + docId);

        String done = client.waitForJob(jobId, Duration.ofMinutes(2));
        System.out.println("job status: " + DocAutoClient.jsonString(done, "status"));
        System.out.println("fields: " + client.getFields(docId));

        byte[] csv = client.downloadResults(docId, "csv");
        System.out.println("csv export bytes: " + csv.length);
    }
}
