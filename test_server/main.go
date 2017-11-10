package main

import (
	"net/http"
	"time"
)

func main() {

	var srv http.Server
	srv.Addr = ":8080"

	http.HandleFunc("/", index_main)

	srv.ListenAndServeTLS("test_server/certs/localhost.cert", "test_server/certs/localhost.key")

}

func index_main(w http.ResponseWriter, r *http.Request) {

	time.Sleep(2000 * time.Millisecond)

	w.Header().Set("Access-Control-Allow-Origin", "*")

	// allow pre-flight headers
	w.Header().Set("Access-Control-Allow-Headers", "Content-Range, Content-Disposition, Content-Type, ETag")

	r.Write(w)
}
